#!/usr/bin/env python
# -*- encoding: utf-8 -*-

try:
    import rados
    import rbd
except ModuleNotFoundError:
    from backy2.io.lib import rados
    from backy2.io.lib import rbd
    print("WARNING: You are using very old ceph libraries. As they might work, they are slower than newer ones.")
    print("Please install python3-rbd and python3-rados.")

from backy2.logging import logger
from backy2.io import IO as _IO
from functools import reduce
from operator import or_
import queue
import re
import threading
import time

STATUS_NOTHING = 0
STATUS_READING = 1
STATUS_WRITING = 2

class IO(_IO):
    pool_name = None
    image_name = None
    snapshot_name = None
    mode = None
    _write_rbd = None
    WRITE_QUEUE_LENGTH = 20
    READ_QUEUE_LENGTH = 20

    def __init__(self, config, block_size, hash_function):
        self.simultaneous_reads = config.getint('simultaneous_reads', 10)
        self.simultaneous_writes = config.getint('simultaneous_reads', 1)

        ceph_conffile = config.get('ceph_conffile')
        self.block_size = block_size
        self.hash_function = hash_function
        cluster_name = config.get('cluster_name', 'ceph')
        rados_name = config.get('rados_name', 'client.admin')
        self.cluster = rados.Rados(conffile=ceph_conffile, name=rados_name, clustername=cluster_name)
        self.cluster.connect()
        # create a bitwise or'd list of the configured features
        self.new_image_features = reduce(or_, [getattr(rbd, feature) for feature in config.getlist('new_image_features')])

        self._reader_threads = []
        self._writer_threads = []

        self.reader_thread_status = {}
        self.writer_thread_status = {}
        self._write_queue = queue.Queue(self.simultaneous_writes + self.WRITE_QUEUE_LENGTH)  # blocks to be written
        self._inqueue = queue.Queue()  # infinite size for all the blocks
        self._outqueue = queue.Queue(self.simultaneous_reads)


    def open_r(self, io_name):
        # io_name has the form rbd://pool/imagename@snapshotname or rbd://pool/imagename
        self.mode = 'r'
        self.io_name = io_name
        img_name = re.match('^rbd://([^/]+)/([^@]+)@?(.+)?$', io_name)
        if not img_name:
            raise RuntimeError('Not a valid io name: {} . Need pool/imagename or pool/imagename@snapshotname'.format(io_name))
        self.pool_name, self.image_name, self.snapshot_name = img_name.groups()
        # try opening it and quit if that's not possible.
        try:
            ioctx = self.cluster.open_ioctx(self.pool_name)
        except rados.ObjectNotFound:
            logger.error('Pool not found: {}'.format(self.pool_name))
            exit('Error opening backup source.')

        try:
            rbd.Image(ioctx, self.image_name, self.snapshot_name, read_only=True)
        except rbd.ImageNotFound:
            logger.error('Image/Snapshot not found: {}@{}'.format(self.image_name, self.snapshot_name))
            exit('Error opening backup source.')

        for i in range(self.simultaneous_reads):
            _reader_thread = threading.Thread(target=self._reader, args=(i,))
            _reader_thread.daemon = True
            _reader_thread.start()
            self._reader_threads.append(_reader_thread)
            self.reader_thread_status[i] = STATUS_NOTHING


    def open_w(self, io_name, size=None, force=False):
        """ size is bytes
        """
        self.mode = 'w'
        # io_name has the form rbd://pool/imagename@snapshotname or rbd://pool/imagename
        self.io_name = io_name
        img_name = re.match('^rbd://([^/]+)/([^@]+)$', io_name)
        if not img_name:
            raise RuntimeError('Not a valid io name: {} . Need pool/imagename'.format(io_name))
        self.pool_name, self.image_name = img_name.groups()
        # try opening it and quit if that's not possible.
        try:
            ioctx = self.cluster.open_ioctx(self.pool_name)
        except rados.ObjectNotFound:
            logger.error('Pool not found: {}'.format(self.pool_name))
            exit('Error opening backup source.')

        try:
            rbd.Image(ioctx, self.image_name)
        except rbd.ImageNotFound:
            rbd.RBD().create(ioctx, self.image_name, size, old_format=False, features=self.new_image_features)
        else:
            if not force:
                logger.error('Image already exists: {}'.format(self.image_name))
                exit('Error opening restore target.')
            else:
                if size < self.size():
                    logger.error('Target size is too small. Has {}b, need {}b.'.format(self.size(), size))
                    exit('Error opening restore target.')

        for i in range(self.simultaneous_writes):
            _writer_thread = threading.Thread(target=self._writer, args=(i,))
            _writer_thread.daemon = True
            _writer_thread.start()
            self._writer_threads.append(_writer_thread)
            self.writer_thread_status[i] = STATUS_NOTHING

        ioctx = self.cluster.open_ioctx(self.pool_name)
        self._write_rbd = rbd.Image(ioctx, self.image_name)


    def size(self):
        ioctx = self.cluster.open_ioctx(self.pool_name)
        with rbd.Image(ioctx, self.image_name, self.snapshot_name, read_only=True) as image:
            size = image.size()
        return size


    def _writer(self, id_):
        """ self._write_queue contains a list of (Block, data) to be written.
        """
        while True:
            entry = self._write_queue.get()
            if entry is None:
                logger.debug("IO writer {} finishing.".format(id_))
                break
            block, data, callback = entry

            offset = block.id * self.block_size
            self.writer_thread_status[id_] = STATUS_WRITING
            written = self._write_rbd.write(data, offset, rados.LIBRADOS_OP_FLAG_FADVISE_DONTNEED)
            assert written == len(data)
            self.writer_thread_status[id_] = STATUS_NOTHING
            if callback:
                callback()

            self._write_queue.task_done()


    def _reader(self, id_):
        """ self._inqueue contains block_ids to be read.
        self._outqueue contains (block_id, data, data_checksum)
        """
        ioctx = self.cluster.open_ioctx(self.pool_name)
        with rbd.Image(ioctx, self.image_name, self.snapshot_name, read_only=True) as image:
            while True:
                entry = self._inqueue.get()
                if entry is None:
                    logger.debug("IO {} finishing.".format(id_))
                    self._outqueue.put(None)  # also let the outqueue end
                    self._inqueue.task_done()
                    break
                block_id, read, metadata = entry
                if not read:
                    self._outqueue.put((block_id, None, None, metadata))
                else:
                    offset = block_id * self.block_size
                    t1 = time.time()
                    self.reader_thread_status[id_] = STATUS_READING
                    data = image.read(offset, self.block_size, rados.LIBRADOS_OP_FLAG_FADVISE_DONTNEED)
                    self.reader_thread_status[id_] = STATUS_NOTHING
                    t2 = time.time()
                    if not data:
                        raise RuntimeError('EOF reached on source when there should be data.')

                    data_checksum = self.hash_function(data).hexdigest()

                    self._outqueue.put((block_id, data, data_checksum, metadata))
                self._inqueue.task_done()


    def read(self, block_id, sync=False, read=True, metadata=None):
        """ Adds a read job """
        self._inqueue.put((block_id, read, metadata))
        if sync:
            rblock_id, data, data_checksum, metadata = self.get()
            if rblock_id != block_id:
                raise RuntimeError('Do not mix threaded reading with sync reading!')
            return data


    def get(self):
        d = self._outqueue.get()
        self._outqueue.task_done()
        return d


    def write(self, block, data, callback=None):
        if not self._write_rbd:
            raise RuntimeError('RBD image not open / available.')
        self._write_queue.put((block, data, callback))


    def queue_status(self):
        return {
            'rq_filled': self._outqueue.qsize() / self._outqueue.maxsize,  # 0..1
            'wq_filled': self._write_queue.qsize() / self._write_queue.maxsize,
        }


    def thread_status(self):
        return "IOR: N{} R{}  IOW: N{} W{} QL{}".format(
                len([t for t in self.reader_thread_status.values() if t==STATUS_NOTHING]),
                len([t for t in self.reader_thread_status.values() if t==STATUS_READING]),
                len([t for t in self.writer_thread_status.values() if t==STATUS_NOTHING]),
                len([t for t in self.writer_thread_status.values() if t==STATUS_WRITING]),
                self._write_queue.qsize(),
                )


    def close(self):
        if self.mode == 'r':
            for _reader_thread in self._reader_threads:
                self._inqueue.put(None)  # ends the threads
            for _reader_thread in self._reader_threads:
                _reader_thread.join()
        elif self.mode == 'w':
            for _writer_thread in self._writer_threads:
                self._write_queue.put(None)  # ends the threads
            for _writer_thread in self._writer_threads:
                _writer_thread.join()
            self._write_rbd.close()

