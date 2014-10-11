from functools import wraps
from time import time, sleep


def timed(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        start = time()
        result = f(*args, **kwargs)
        elapsed = time() - start
        print "%s took %fs to finish" % (f.__name__, elapsed)
        return result
    return wrapper

import logging
import struct
import os.path
from threading import Thread, Event, Timer
from collections import deque
# from multiprocessing import Process as Thread
# from multiprocessing import Event
# from multiprocessing import Queue

import numpy as np
import tables as tb

from utils.utils import get_float_time
from analysis.RawDataConverter.data_struct import MetaTableV2 as MetaTable, generate_scan_parameter_description
from bitstring import BitArray  # TODO: bitarray.bitarray() (in Python3 use int.from_bytes() to convert bitarray to integer)
from collections import OrderedDict

from SiLibUSB import SiUSBDevice

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] (%(threadName)-10s) %(message)s")

data_deque_dict_names = ["data", "timestamp_start", "timestamp_stop", "error"]


class Readout(object):
    def __init__(self, device):
        if isinstance(device, SiUSBDevice):
            self.device = device
        else:
            raise ValueError('Device object is not compatible')
        self.worker_thread = None
        self.data = deque()
        self.stop_thread_event = Event()
        self.stop_thread_event.set()
        self.readout_interval = 0.05
        self.rx_base_address = dict([(idx, addr) for idx, addr in enumerate(range(0x8600, 0x8200, -0x0100))])
        self.sram_base_address = dict([(idx, addr) for idx, addr in enumerate(range(0x8100, 0x8200, 0x0100))])
        self.timestamp = None
        self.update_timestamp()

    def start(self, reset_rx=False, empty_data_queue=True, reset_sram_fifo=True, filename=None):
        if self.worker_thread != None:
            raise RuntimeError('Thread is not None')
        if reset_rx:
            self.reset_rx()
        if empty_data_queue:
            # self.data.empty()
            self.data.clear()
        if reset_sram_fifo:
            self.reset_sram_fifo()
        self.stop_thread_event.clear()
        self.worker_thread = Thread(target=self.worker)
        self.worker_thread.daemon = True
        logging.info('Starting readout')
        self.worker_thread.start()

    def stop(self, timeout=10):
        if self.worker_thread == None:
            raise RuntimeError('Readout thread not existing: use start() before stop()')
        if timeout:
            def stop_thread():
                logging.warning('Waiting for empty SRAM FIFO: timeout after %.1f second(s)' % timeout)
                self.stop_thread_event.set()

            timeout_timer = Timer(timeout, stop_thread)
            timeout_timer.start()

            fifo_size = self.get_sram_fifo_size()
            old_fifo_size = -1
            while (old_fifo_size != fifo_size or fifo_size != 0) and self.worker_thread.is_alive() and not self.stop_thread_event.wait(1.5 * self.readout_interval):
                old_fifo_size = fifo_size
                fifo_size = self.get_sram_fifo_size()

            timeout_timer.cancel()

        self.stop_thread_event.set()  # stop thread when no timeout is set
        self.worker_thread.join()
        self.worker_thread = None
        logging.info('Stopped readout')

    def print_readout_status(self):
        sync_status = self.get_rx_sync_status()
        discard_count = self.get_rx_fifo_discard_count()
        error_count = self.get_rx_8b10b_error_count()
        logging.info('Data queue size: %d' % len(self.data))  # .qsize())
        logging.info('SRAM FIFO size: %d' % self.get_sram_fifo_size())
        logging.info('Channel:                     %s', " | ".join([('CH%d' % channel).rjust(3) for channel in range(1, 5, 1)]))
        logging.info('RX sync:                     %s', " | ".join(["YES".rjust(3) if status == True else "NO".rjust(3) for status in sync_status]))
        logging.info('RX FIFO discard counter:     %s', " | ".join([repr(count).rjust(3) for count in discard_count]))
        logging.info('RX FIFO 8b10b error counter: %s', " | ".join([repr(count).rjust(3) for count in error_count]))
        if not any(self.get_rx_sync_status()) or any(discard_count) or any(error_count):
            logging.warning('RX errors detected')

    def worker(self):
        '''Reading thread to continuously reading SRAM

        Worker thread function that uses read_data_dict() and appends data to self.data (collection.deque)
        '''
        # TODO: check FIFO status (overflow) and check rx status (sync) once in a while
        while not self.stop_thread_event.wait(self.readout_interval):  # TODO: this is probably what you need to reduce processor cycles
            self.device.lock.acquire()
            try:
                data = self.read_data_dict()
            except Exception as e:
                logging.error('Stopping readout: %s' % (e))
                self.stop_thread_event.set()  # stop readout on any occurring exception
                continue
            finally:
                self.device.lock.release()
            if data["data"].shape[0] > 0:  # TODO: make it optional
                self.data.append(data)  # put({'timestamp':get_float_time(), 'raw_data':filtered_data_words, 'error':0})

    def read_data_dict(self):
        '''Read single to read SRAM once

        Can be used without threading.

        Returns
        -------
        dict with following keys: "data", "timestamp_start", "timestamp_stop", "error"
        '''
        last_time, curr_time = self.update_timestamp()
        return {"data": self.read_data(), "timestamp_start": last_time, "timestamp_stop": curr_time, "error": self.read_status()}

    def update_timestamp(self):
        curr_time = get_float_time()
        last_time = self.timestamp
        self.timestamp = curr_time
        return last_time, curr_time

    def read_data(self):
        '''Read SRAM data words (array of 32-bit uint data words)

        Can be used without threading

        Returns
        -------
        numpy.array
        '''
        # TODO: check FIFO status (overflow) and check rx status (sync) once in a while

        fifo_size = self.get_sram_fifo_size()
        if fifo_size % 2 == 1:  # sometimes a read happens during writing, but we want to have a multiplicity of 32 bits
            fifo_size -= 1
            # print "FIFO size odd"
        if fifo_size > 0:
            # old style:
            # fifo_data = self.device.FastBlockRead(4*fifo_size/2)
            # data_words = struct.unpack('>'+fifo_size/2*'I', fifo_data)
            return np.fromstring(self.device.FastBlockRead(4 * fifo_size / 2).tostring(), dtype=np.dtype('>u4'))
        else:
            return np.array([], dtype=np.dtype('>u4'))  # create empty array
            # return np.empty(0, dtype=np.dtype('>u4')) # FIXME: faster?

    def read_status(self):
        return 0

    def reset_sram_fifo(self):
        logging.info('Resetting SRAM FIFO')
        self.update_timestamp()
        self.device.WriteExternal(address=self.sram_base_address[0], data=[0])
        sleep(0.2)  # sleep here for a while
        if self.get_sram_fifo_size() != 0:
            logging.warning('SRAM FIFO size not zero')

    def get_sram_fifo_size(self):
        retfifo = self.device.ReadExternal(address=self.sram_base_address[0] + 1, size=3)
        retfifo.append(0)  # 4 bytes
        return struct.unpack_from('I', retfifo)[0]

    def reset_rx(self, channels=None):
        logging.info('Resetting RX')
        if channels == None:
            channels = self.rx_base_address.iterkeys()
        filter(lambda i: self.device.WriteExternal(address=self.rx_base_address[i] + 1, data=[0]), channels)  # reset RX counters
        # since WriteExternal returns nothing, filter returns empty list
        sleep(0.1)  # sleep here for a while

    def get_rx_sync_status(self, channels=None):
        if channels == None:
            channels = self.rx_base_address.iterkeys()
        return map(lambda i: True if (self.device.ReadExternal(address=self.rx_base_address[i] + 2, size=1)[0]) & 0x1 == 1 else False, channels)

    def get_rx_8b10b_error_count(self, channels=None):
        if channels == None:
            channels = self.rx_base_address.iterkeys()
        return map(lambda i: self.device.ReadExternal(address=self.rx_base_address[i] + 5, size=1)[0], channels)

    def get_rx_fifo_discard_count(self, channels=None):
        if channels == None:
            channels = self.rx_base_address.iterkeys()
        return map(lambda i: self.device.ReadExternal(address=self.rx_base_address[i] + 6, size=1)[0], channels)


def convert_data_array(array, filter_func=None, converter_func=None):
    '''Filter and convert data array (numpy.ndarray)

    Parameters
    ----------
    array : numpy.array
        Raw data array.
    filter_func : function
        Function that takes array and returns true or false for each item in array.
    converter_func : function
        Function that takes array and returns an array or tuple of arrays.

    Returns
    -------
    array of specified dimension (converter_func) and content (filter_func)
    '''
#     if filter_func != None:
#         if not hasattr(filter_func, '__call__'):
#             raise ValueError('Filter is not callable')
    if filter_func:
        array = array[filter_func(array)]
#     if converter_func != None:
#         if not hasattr(converter_func, '__call__'):
#             raise ValueError('Converter is not callable')
    if converter_func:
        array = converter_func(array)
    return array


def data_array_from_data_dict_iterable(data_dict_iterable, clear_deque=False):
    '''Convert data dictionary iterable (e.g. data deque)

    Parameters
    ----------
    data_dict_iterable : iterable
        Iterable (e.g. list, deque, ...) where each element is a dict with following keys: "data", "timestamp_start", "timestamp_stop", "error"
    clear_deque : bool
        Clear deque when returning.

    Returns
    -------
    data_array : numpy.array
        concatenated data array
    '''
    try:
        data_array = np.concatenate([item["data"] for item in data_dict_iterable])
    except ValueError:
        data_array = np.array([], dtype=np.dtype('>u4'))
    if clear_deque:
        data_dict_iterable.clear()
    return data_array


def data_dict_list_from_data_dict_iterable(data_dict_iterable, filter_func=None, converter_func=None, concatenate=False, clear_deque=False):  # TODO: implement concatenate
    '''Convert data dictionary iterable (e.g. data deque)

    Parameters
    ----------
    data_dict_iterable : iterable
        Iterable (e.g. list, deque, ...) where each element is a dict with following keys: "data", "timestamp_start", "timestamp_stop", "error"
    filter_func : function
        Function that takes array and returns true or false for each item in array.
    converter_func : function
        Function that takes array and returns an array or tuple of arrays.
    concatenate: bool
        Concatenate input arrays. If true, returns single dict.
    clear_deque : bool
        Clear deque when returning.

    Returns
    -------
    data dictionary list of the form [{"data":converted_data, "timestamp_start":ts_start, "timestamp_stop":ts_stop, "error":error}, {...}, ...]
    '''
    data_dict_list = []
    for item in data_dict_iterable:
        data_dict_list.append({"data": convert_data_array(item["data"], filter_func=filter_func, converter_func=converter_func), "timestamp_start": item["timestamp_start"], "timestamp_stop": item["timestamp_stop"], "error": item["error"]})
    if clear_deque:
        data_dict_iterable.clear()
    return data_dict_list


def is_data_from_channel(channel=4):  # function factory
    '''Select data from channel

    Parameters:
    channel : int
        Channel number (4 is default channel on Single Chip Card)

    Returns:
    Function

    Usage:
    # 1
    is_data_from_channel_4 = is_data_from_channel(4)
    data_from_channel_4 = data_array[is_data_from_channel_4(data_array)]
    # 2
    filter_func = logical_and(is_data_record, is_data_from_channel(3))
    data_record_from_channel_3 = data_array[filter_func(data_array)]
    # 3
    is_raw_data_from_channel_3 = is_data_from_channel(3)(raw_data)

    Similar to:
    f_ch3 = functoools.partial(is_data_from_channel, channel=3)
    l_ch4 = lambda x: is_data_from_channel(x, channel=4)

    Note:
    Trigger data not included
    '''
    if channel > 0 and channel < 5:
        def f(value):
            return np.equal(np.right_shift(np.bitwise_and(value, 0x7F000000), 24), channel)
        f.__name__ = "is_data_from_channel_" + str(channel)  # or use inspect module: inspect.stack()[0][3]
        return f
    else:
        raise ValueError('Invalid channel number')


def logical_and(f1, f2):  # function factory
    '''Logical and from functions.

    Parameters
    ----------
    f1, f2 : function
        Function that takes array and returns true or false for each item in array.

    Returns
    -------
    Function

    Examples
    --------
    filter_func=logical_and(is_data_record, is_data_from_channel(4))  # new filter function
    filter_func(array) # array that has Data Records from channel 4
    '''
    def f(value):
        return np.logical_and(f1(value), f2(value))
    f.__name__ = f1.__name__ + "_and_" + f2.__name__
    return f


def logical_or(f1, f2):  # function factory
    '''Logical or from functions.

    Parameters
    ----------
    f1, f2 : function
        Function that takes array and returns true or false for each item in array.

    Returns
    -------
    Function
    '''
    def f(value):
        return np.logical_or(f1(value), f2(value))
    f.__name__ = f1.__name__ + "_or_" + f2.__name__
    return f


def logical_not(f):  # function factory
    '''Logical not from functions.

    Parameters
    ----------
    f1, f2 : function
        Function that takes array and returns true or false for each item in array.

    Returns
    -------
    Function
    '''
    def f(value):
        return np.logical_not(f(value))
    f.__name__ = "not_" + f.__name__
    return f


def logical_xor(f1, f2):  # function factory
    '''Logical xor from functions.

    Parameters
    ----------
    f1, f2 : function
        Function that takes array and returns true or false for each item in array.

    Returns
    -------
    Function
    '''
    def f(value):
        return np.logical_xor(f1(value), f2(value))
    f.__name__ = f1.__name__ + "_xor_" + f2.__name__
    return f


def is_fe_record(value):
    return not is_trigger_data(value) and not is_status_data(value)


def is_data_header(value):
    return np.equal(np.bitwise_and(value, 0x00FF0000), 0b111010010000000000000000)


def is_address_record(value):
    return np.equal(np.bitwise_and(value, 0x00FF0000), 0b111010100000000000000000)


def is_value_record(value):
    return np.equal(np.bitwise_and(value, 0x00FF0000), 0b111011000000000000000000)


def is_service_record(value):
    return np.equal(np.bitwise_and(value, 0x00FF0000), 0b111011110000000000000000)


def is_data_record(value):
    return np.logical_and(np.logical_and(np.less_equal(np.bitwise_and(value, 0x00FE0000), 0x00A00000), np.less_equal(np.bitwise_and(value, 0x0001FF00), 0x00015000)), np.logical_and(np.not_equal(np.bitwise_and(value, 0x00FE0000), 0x00000000), np.not_equal(np.bitwise_and(value, 0x0001FF00), 0x00000000)))


def is_status_data(value):
    '''Select status data
    '''
    return np.equal(np.bitwise_and(value, 0xFF000000), 0x00000000)


def is_trigger_data(value):
    '''Select trigger data (trigger number)
    '''
    return np.equal(np.bitwise_and(value, 0xFF000000), 0x80000000)


def is_tdc_data(value):
    '''Select tdc data
    '''
    return np.equal(np.bitwise_and(value, 0xF0000000), 0x40000000)


def get_address_record_address(value):
    '''Returns the address in the address record
    '''
    return np.bitwise_and(value, 0x0000EFFF)


def get_address_record_type(value):
    '''Returns the type in the address record
    '''
    return np.right_shift(np.bitwise_and(value, 0x00008000), 14)


def get_value_record(value):
    '''Returns the value in the value record
    '''
    return np.bitwise_and(value, 0x0000FFFF)

# def def get_col_row_tot_array_from_data_record_array(max_tot=14):


def get_col_row_tot_array_from_data_record_array(array):
    '''Convert raw data array to column, row, and ToT array

    Parameters
    ----------
    array : numpy.array
        Raw data array.

    Returns
    -------
    Tuple of arrays.
    '''
    def get_col_row_tot_1_array_from_data_record_array(value):
        return np.right_shift(np.bitwise_and(value, 0x00FE0000), 17), np.right_shift(np.bitwise_and(value, 0x0001FF00), 8), np.right_shift(np.bitwise_and(value, 0x000000F0), 4)
#         return (value & 0xFE0000)>>17, (value & 0x1FF00)>>8, (value & 0x0000F0)>>4 # numpy.vectorize()

    def get_col_row_tot_2_array_from_data_record_array(value):
        return np.right_shift(np.bitwise_and(value, 0x00FE0000), 17), np.add(np.right_shift(np.bitwise_and(value, 0x0001FF00), 8), 1), np.bitwise_and(value, 0x0000000F)
#         return (value & 0xFE0000)>>17, ((value & 0x1FF00)>>8)+1, (value & 0x0000F) # numpy.vectorize()

    col_row_tot_1_array = np.column_stack(get_col_row_tot_1_array_from_data_record_array(array))
    col_row_tot_2_array = np.column_stack(get_col_row_tot_2_array_from_data_record_array(array))
#     print col_row_tot_1_array, col_row_tot_1_array.shape, col_row_tot_1_array.dtype
#     print col_row_tot_2_array, col_row_tot_2_array.shape, col_row_tot_2_array.dtype
    # interweave array here
    col_row_tot_array = np.vstack((col_row_tot_1_array.T, col_row_tot_2_array.T)).reshape((3, -1), order='F').T  # http://stackoverflow.com/questions/5347065/interweaving-two-numpy-arrays
#     print col_row_tot_array, col_row_tot_array.shape, col_row_tot_array.dtype
    # remove ToT > 14 (late hit, no hit) from array, remove row > 336 in case we saw hit in row 336 (no double hit possible)
    try:
        col_row_tot_array_filtered = col_row_tot_array[col_row_tot_array[:, 2] < 14]  # [np.logical_and(col_row_tot_array[:,2]<14, col_row_tot_array[:,1]<=336)]
#         print col_row_tot_array_filtered, col_row_tot_array_filtered.shape, col_row_tot_array_filtered.dtype
    except IndexError:
        # logging.warning('Array is empty')
        return np.array([], dtype=np.dtype('>u4')), np.array([], dtype=np.dtype('>u4')), np.array([], dtype=np.dtype('>u4'))
    return col_row_tot_array_filtered[:, 0], col_row_tot_array_filtered[:, 1], col_row_tot_array_filtered[:, 2]  # column, row, ToT


def get_col_row_array_from_data_record_array(array):
    col, row, _ = get_col_row_tot_array_from_data_record_array(array)
    return col, row


def get_row_col_array_from_data_record_array(array):
    col, row, _ = get_col_row_tot_array_from_data_record_array(array)
    return row, col


def get_tot_array_from_data_record_array(array):
    _, _, tot = get_col_row_tot_array_from_data_record_array(array)
    return tot


def get_occupancy_mask_from_data_record_array(array, occupancy):
    pass  # TODO:


def get_col_row_iterator_from_data_records(array):  # generator
    for item in np.nditer(array):  # , flags=['multi_index']):
        yield np.right_shift(np.bitwise_and(item, 0x00FE0000), 17), np.right_shift(np.bitwise_and(item, 0x0001FF00), 8)
        if np.not_equal(np.bitwise_and(item, 0x0000000F), 15):
            yield np.right_shift(np.bitwise_and(item, 0x00FE0000), 17), np.add(np.right_shift(np.bitwise_and(item, 0x0001FF00), 8), 1)


def get_row_col_iterator_from_data_records(array):  # generator
    for item in np.nditer(array, flags=['multi_index']):
        yield np.right_shift(np.bitwise_and(item, 0x0001FF00), 8), np.right_shift(np.bitwise_and(item, 0x00FE0000), 17)
        if np.not_equal(np.bitwise_and(item, 0x0000000F), 15):
            yield np.add(np.right_shift(np.bitwise_and(item, 0x0001FF00), 8), 1), np.right_shift(np.bitwise_and(item, 0x00FE0000), 17)


def get_col_row_tot_iterator_from_data_records(array):  # generator
    for item in np.nditer(array, flags=['multi_index']):
        yield np.right_shift(np.bitwise_and(item, 0x00FE0000), 17), np.right_shift(np.bitwise_and(item, 0x0001FF00), 8), np.right_shift(np.bitwise_and(item, 0x000000F0), 4)  # col, row, ToT1
        if np.not_equal(np.bitwise_and(item, 0x0000000F), 15):
            yield np.right_shift(np.bitwise_and(item, 0x00FE0000), 17), np.add(np.right_shift(np.bitwise_and(item, 0x0001FF00), 8), 1), np.bitwise_and(item, 0x0000000F)  # col, row+1, ToT2


def get_tot_iterator_from_data_records(array):  # generator
    for item in np.nditer(array, flags=['multi_index']):
        yield np.right_shift(np.bitwise_and(item, 0x000000F0), 4)  # ToT1
        if np.not_equal(np.bitwise_and(item, 0x0000000F), 15):
            yield np.bitwise_and(item, 0x0000000F)  # ToT2


def open_raw_data_file(filename, mode="a", title="", scan_parameters=[], **kwargs):
    '''Mimics pytables.open_file()/openFile()

    Returns:
    RawDataFile Object

    Examples:
    with open_raw_data_file(filename = self.scan_data_filename, title=self.scan_id, scan_parameters=[scan_parameter]) as raw_data_file:
        # do something here
        raw_data_file.append(self.readout.data, scan_parameters={scan_parameter:scan_parameter_value})
    '''
    return RawDataFile(filename=filename, mode=mode, title=title, scan_parameters=scan_parameters, **kwargs)


class RawDataFile(object):
    '''Saving raw data file from data dictionary iterable (e.g. data deque)

    TODO: Python 3.x support for contextlib.ContextDecorator
    '''
    def __init__(self, filename, mode="a", title="", scan_parameters=[], **kwargs):  # mode="r+" to append data, raw_data_file_h5 must exist, "w" to overwrite raw_data_file_h5, "a" to append data, if raw_data_file_h5 does not exist it is created):
        self.filename = filename
        self.scan_parameters = scan_parameters
        self.raw_data_earray = None
        self.meta_data_table = None
        self.scan_param_table = None
        self.raw_data_file_h5 = None
        self.open(mode, title, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False  # do not hide exceptions

    def open(self, mode='a', title='', **kwargs):
        if os.path.splitext(self.filename)[1].strip().lower() != ".h5":
            self.filename = os.path.splitext(self.filename)[0] + ".h5"
        if os.path.isfile(self.filename) and mode in ('r+', 'a'):
            logging.info('Opening existing raw data file: %s' % self.filename)
        else:
            logging.info('Opening new raw data file: %s' % self.filename)

        filter_raw_data = tb.Filters(complib='blosc', complevel=5, fletcher32=False)
        filter_tables = tb.Filters(complib='zlib', complevel=5, fletcher32=False)
        self.raw_data_file_h5 = tb.openFile(self.filename, mode=mode, title=title, **kwargs)
        try:
            self.raw_data_earray = self.raw_data_file_h5.createEArray(self.raw_data_file_h5.root, name='raw_data', atom=tb.UIntAtom(), shape=(0,), title='raw_data', filters=filter_raw_data)  # expectedrows = ???
        except tb.exceptions.NodeError:
            self.raw_data_earray = self.raw_data_file_h5.getNode(self.raw_data_file_h5.root, name='raw_data')
        try:
            self.meta_data_table = self.raw_data_file_h5.createTable(self.raw_data_file_h5.root, name='meta_data', description=MetaTable, title='meta_data', filters=filter_tables)
        except tb.exceptions.NodeError:
            self.meta_data_table = self.raw_data_file_h5.getNode(self.raw_data_file_h5.root, name='meta_data')
        if self.scan_parameters:
            try:
                scan_param_descr = generate_scan_parameter_description(self.scan_parameters)
                self.scan_param_table = self.raw_data_file_h5.createTable(self.raw_data_file_h5.root, name='scan_parameters', description=scan_param_descr, title='scan_parameters', filters=filter_tables)
            except tb.exceptions.NodeError:
                self.scan_param_table = self.raw_data_file_h5.getNode(self.raw_data_file_h5.root, name='scan_parameters')

    def close(self):
        self.flush()
        logging.info('Closing raw data file: %s' % self.filename)
        self.raw_data_file_h5.close()

    def append(self, data_dict_iterable, scan_parameters={}, clear_deque=False, flush=True, **kwargs):
#         if not data_dict_iterable:
#             logging.warning('Iterable is empty')
        row_meta = self.meta_data_table.row
        if scan_parameters:
            row_scan_param = self.scan_param_table.row

        total_words_before = self.raw_data_earray.nrows

        def append_item(item):
            total_words = self.raw_data_earray.nrows
            raw_data = item["data"]
            len_raw_data = raw_data.shape[0]
            self.raw_data_earray.append(raw_data)
#             row_meta['timestamp'] = item["timestamp_stop"]
            row_meta['timestamp_start'] = item["timestamp_start"]
            row_meta['timestamp_stop'] = item["timestamp_stop"]
            row_meta['error'] = item["error"]
#             row_meta['length'] = len_raw_data
            row_meta['data_length'] = len_raw_data
#             row_meta['start_index'] = total_words
            row_meta['index_start'] = total_words
            total_words += len_raw_data
#             row_meta['stop_index'] = total_words
            row_meta['index_stop'] = total_words
            row_meta.append()
            if self.scan_parameters:
                for key, value in dict.iteritems(scan_parameters):
                    row_scan_param[key] = value
                row_scan_param.append()

#         if clear_deque:
#             while True:
#                 try:
#                     item = data_dict_iterable.popleft()
#                 except IndexError:
#                     break
#                 append_item(item)
#
#         else:
        for item in data_dict_iterable:
            append_item(item)

        total_words_after = self.raw_data_earray.nrows
        if total_words_after == total_words_before:
            logging.info('Nothing to append: %s' % self.filename)

        if clear_deque:
            data_dict_iterable.clear()

        if flush:
            self.flush()

    def flush(self):
        self.raw_data_earray.flush()
        self.meta_data_table.flush()
        if self.scan_parameters:
            self.scan_param_table.flush()


def save_raw_data_from_data_dict_iterable(data_dict_iterable, filename, mode='a', title='', scan_parameters={}, **kwargs):  # mode="r+" to append data, raw_data_file_h5 must exist, "w" to overwrite raw_data_file_h5, "a" to append data, if raw_data_file_h5 does not exist it is created
    '''Writing raw data file from data dictionary iterable (e.g. data deque)

    If you need to write raw data once in a while this function may make it easy for you.
    '''
    with open_raw_data_file(filename, mode='a', title='', scan_parameters=list(dict.iterkeys(scan_parameters)), **kwargs) as raw_data_file:
        raw_data_file.append(data_dict_iterable, scan_parameters=scan_parameters, **kwargs)


class FEI4Record(object):
    """Record Object

    """
    def __init__(self, data_word, chip_flavor):
        self.record_rawdata = int(data_word) & 0x00FFFFFF
        self.chip_flavor = str(chip_flavor).lower()
        self.chip_flavors = ['fei4a', 'fei4b']
        if self.chip_flavor not in self.chip_flavors:
            raise KeyError('Chip flavor is not of type {}'.format(', '.join('\'' + flav + '\'' for flav in self.chip_flavors)))
        self.record_word = BitArray(uint=self.record_rawdata, length=24)
        self.record_dict = None
        if is_data_header(self.record_rawdata):
            self.record_type = "DH"
            if self.chip_flavor == "fei4a":
                self.record_dict = OrderedDict([('start', self.record_word[0:5].uint), ('header', self.record_word[5:8].uint), ('flag', self.record_word[8:9].uint), ('lvl1id', self.record_word[9:16].uint), ('bcid', self.record_word[16:24].uint)])
            elif self.chip_flavor == "fei4b":
                self.record_dict = OrderedDict([('start', self.record_word[0:5].uint), ('header', self.record_word[5:8].uint), ('flag', self.record_word[8:9].uint), ('lvl1id', self.record_word[9:14].uint), ('bcid', self.record_word[14:24].uint)])
        elif is_address_record(self.record_rawdata):
            self.record_type = "AR"
            self.record_dict = OrderedDict([('start', self.record_word[0:5].uint), ('header', self.record_word[5:8].uint), ('type', self.record_word[8:9].uint), ('address', self.record_word[9:24].uint)])
        elif is_value_record(self.record_rawdata):
            self.record_type = "VR"
            self.record_dict = OrderedDict([('start', self.record_word[0:5].uint), ('header', self.record_word[5:8].uint), ('value', self.record_word[8:24].uint)])
        elif is_service_record(self.record_rawdata):
            self.record_type = "SR"
            if self.chip_flavor == "fei4a":
                self.record_dict = OrderedDict([('start', self.record_word[0:5].uint), ('header', self.record_word[5:8].uint), ('code', self.record_word[8:14].uint), ('counter', self.record_word[14:24].uint)])
            elif self.chip_flavor == "fei4b":
                if self.record_word[8:14].uint == 14:
                    self.record_dict = OrderedDict([('start', self.record_word[0:5].uint), ('header', self.record_word[5:8].uint), ('code', self.record_word[8:14].uint), ('lvl1id', self.record_word[14:21].uint), ('bcid', self.record_word[21:24].uint)])
                elif self.record_word[8:14].uint == 15:
                    self.record_dict = OrderedDict([('start', self.record_word[0:5].uint), ('header', self.record_word[5:8].uint), ('code', self.record_word[8:14].uint), ('skipped', self.record_word[14:24].uint)])
                elif self.record_word[8:14].uint == 16:
                    self.record_dict = OrderedDict([('start', self.record_word[0:5].uint), ('header', self.record_word[5:8].uint), ('code', self.record_word[8:14].uint), ('truncation flag', self.record_word[14:15].uint), ('truncation counter', self.record_word[15:20].uint), ('l1req', self.record_word[20:24].uint)])
                else:
                    self.record_dict = OrderedDict([('start', self.record_word[0:5].uint), ('header', self.record_word[5:8].uint), ('code', self.record_word[8:14].uint), ('counter', self.record_word[14:24].uint)])
        elif is_data_record(self.record_rawdata):
            self.record_type = "DR"
            self.record_dict = OrderedDict([('column', self.record_word[0:7].uint), ('row', self.record_word[7:16].uint), ('tot1', self.record_word[16:20].uint), ('tot2', self.record_word[20:24].uint)])
        else:
            self.record_type = "UNKNOWN"
            self.record_dict = OrderedDict([('unknown', self.record_word.uint)])
#             raise ValueError('Unknown data word: '+str(self.record_word.uint))

    def __len__(self):
        return len(self.record_dict)

    def __getitem__(self, key):
        if not (isinstance(key, (int, long)) or isinstance(key, basestring)):
            raise TypeError()
        try:
            return self.record_dict[key.lower()]
        except TypeError:
            return self.record_dict[self.record_dict.iterkeys()[int(key)]]

    def next(self):
        return self.record_dict.iteritems().next()

    def __iter__(self):
        return self.record_dict.iteritems()

    def __eq__(self, other):
        try:
            return self.record_type.lower() == other.lower()
        except:
            try:
                return self.record_type == other.record_type
            except:
                return False

    def __str__(self):
        return self.record_type + ' {}'.format(' '.join(key + ':' + str(val) for key, val in self.record_dict.iteritems()))

    def __repr__(self):
        return repr(self.__str__())