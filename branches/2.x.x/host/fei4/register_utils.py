import time
import numpy as np
import re
import logging

from utils.utils import bitarray_to_array
from daq.readout_utils import interpret_pixel_data

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] (%(threadName)-10s) %(message)s")


class FEI4RegisterUtils(object):
    def __init__(self, dut, readout, register):
        self.dut = dut
        self.readout = readout
        self.register = register
        self.command_memory_byte_size = 2048 - 16  # 16 bytes of register data
        self.zero_cmd_length = 1
        self.zero_cmd = self.register.get_commands("zeros", length=self.zero_cmd_length)[0]
        self.zero_cmd_padded = self.zero_cmd.copy()
        self.zero_cmd_padded.fill()

    def add_commands(self, x, y):
            return x + self.zero_cmd + y  # FE needs a zero bits between commands

    def add_byte_padded_commands(self, x, y):
            x_fill = x.copy()
            x_fill.fill()
            y_fill = y.copy()
            y_fill.fill()
            return x_fill + self.zero_cmd_padded + y_fill  # FE needs a zero between commands

    def concatenate_commands(self, commands, byte_padding=False):
        if byte_padding:
            return reduce(self.add_byte_padded_commands, commands)
        else:
            return reduce(self.add_commands, commands)

    def send_commands(self, commands, repeat=1, wait_for_finish=True, concatenate=True, byte_padding=False, clear_memory=False):
        if concatenate:
            commands_iter = iter(commands)
            concatenated_cmd = commands_iter.next()
            for command in commands_iter:
                concatenated_cmd_tmp = self.concatenate_commands((concatenated_cmd, command), byte_padding=byte_padding)
                if concatenated_cmd_tmp.length() > self.command_memory_byte_size * 8:
                    self.send_command(command=concatenated_cmd, repeat=repeat, wait_for_finish=wait_for_finish, set_length=True, clear_memory=clear_memory)
                    concatenated_cmd = command
                else:
                    concatenated_cmd = concatenated_cmd_tmp
            # send remaining commands
            self.send_command(command=concatenated_cmd, repeat=repeat, wait_for_finish=wait_for_finish, set_length=True, clear_memory=clear_memory)
        else:
            max_length = 0
            if repeat:
                self.dut['cmd']['CMD_REPEAT'] = repeat
            for command in commands:
                max_length = max(command.length(), max_length)
                self.send_command(command=command, repeat=None, wait_for_finish=wait_for_finish, set_length=True, clear_memory=False)
            if clear_memory:
                self.clear_command_memory(length=max_length)

    def send_command(self, command, repeat=1, wait_for_finish=True, set_length=True, clear_memory=False):
        if repeat:
            self.dut['cmd']['CMD_REPEAT'] = repeat
        # write command into memory
        command_length = self.set_command(command, set_length=set_length)
        # sending command
        self.dut['cmd']['START']
        # wait for command to be finished
        if wait_for_finish:
            self.wait_for_command(length=command_length, repeat=repeat)
        # clear command memory
        if clear_memory:
            self.clear_command_memory(length=command_length)

    def clear_command_memory(self, length=None):
        self.set_command(self.register.get_commands("zeros", length=(self.command_memory_byte_size * 8) if length is None else length)[0], set_length=False)

    def set_command(self, command, set_length=True, byte_offset=0):
        command_length = command.length()
        # set command bit length
        if set_length:
            self.dut['cmd']['CMD_SIZE'] = command_length
        # set command
        data = bitarray_to_array(command)
        self.dut['cmd'].set_data(data=data, addr=byte_offset)
        return command_length

    def wait_for_command(self, length=None, repeat=None):
        if length is not None:
            if repeat is None:
                repeat = 1
            # print 'sleeping'
            time.sleep((length + 500) * 0.000000025 * repeat)  # TODO: optimize wait time
        while not self.is_ready:
            pass

    @property
    def is_ready(self):
        return True if self.dut['cmd']['READY'] else False

    def global_reset(self):
        '''FEI4 Global Reset

        Special function to do a global reset on FEI4. Sequence of commands has to be like this, otherwise FEI4B will be left in weird state.
        '''
        logging.info('Sending Global Reset')
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        commands.extend(self.register.get_commands("globalreset"))
        self.send_commands(commands)
        time.sleep(0.1)
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        commands.extend(self.register.get_commands("runmode"))
        self.send_commands(commands)

    def reset_service_records(self):
        '''Resetting Service Records

        This will reset Service Record counters. This will also bring back alive some FE where the output FIFO is stuck (no data is coming out in run mode).
        '''
        logging.info('Resetting Service Records')
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        self.register.set_global_register_value('ReadErrorReq', 1)
        commands.extend(self.register.get_commands("wrregister", name=['ReadErrorReq']))
        commands.extend(self.register.get_commands("globalpulse", width=0))
        self.register.set_global_register_value('ReadErrorReq', 0)
        commands.extend(self.register.get_commands("wrregister", name=['ReadErrorReq']))
        commands.extend(self.register.get_commands("runmode"))
        self.send_commands(commands)

    def reset_bunch_counter(self):
        '''Resetting Bunch Counter
        '''
        logging.info('Resetting Bunch Counter')
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        commands.extend(self.register.get_commands("BCR"))
        self.send_commands(commands)
        time.sleep(0.1)
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        commands.extend(self.register.get_commands("runmode"))
        self.send_commands(commands)

    def reset_event_counter(self):
        '''Resetting Event Counter
        '''
        logging.info('Resetting Event Counter')
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        commands.extend(self.register.get_commands("ECR"))  # wait some time after ECR
        self.send_commands(commands)
        time.sleep(0.1)
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        commands.extend(self.register.get_commands("runmode"))
        self.send_commands(commands)

    def configure_all(self, same_mask_for_all_dc=False):
        self.configure_global()
        self.configure_pixel(same_mask_for_all_dc=same_mask_for_all_dc)

    def configure_global(self):
        logging.info('Sending global configuration to FE')
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        commands.extend(self.register.get_commands("wrregister", readonly=False))
        commands.extend(self.register.get_commands("runmode"))
        self.send_commands(commands, concatenate=True)

    def configure_pixel(self, same_mask_for_all_dc=False):
        logging.info('Sending pixel configuration to FE')
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        commands.extend(self.register.get_commands("wrfrontend", same_mask_for_all_dc=False, name=["TDAC", "FDAC"]))  # same config for all DC is in general a not so good idea
        commands.extend(self.register.get_commands("wrfrontend", same_mask_for_all_dc=same_mask_for_all_dc, name=["Imon", "Enable", "c_high", "c_low"]))
        commands.extend(self.register.get_commands("wrfrontend", same_mask_for_all_dc=same_mask_for_all_dc, name=["EnableDigInj"]))  # write EnableDigInj last
        commands.extend(self.register.get_commands("runmode"))
        self.send_commands(commands)

    def set_gdac(self, value):
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        if self.register.fei4b:
            altf = value & 0xff
            altc = (value >> 7)
            altc &= ~0x01
            self.register.set_global_register_value("Vthin_AltCoarse", altc)  # take every second AltCoarse value
            self.register.set_global_register_value("Vthin_AltFine", altf)  # take low word
        else:
            altf = value & 0xff
            altc = (value >> 8)
            self.register.set_global_register_value("Vthin_AltCoarse", altc)  # take high word
            self.register.set_global_register_value("Vthin_AltFine", altf)  # take low word
        commands.extend(self.register.get_commands("wrregister", name=["Vthin_AltFine", "Vthin_AltCoarse"]))
        commands.extend(self.register.get_commands("runmode"))
        self.send_commands(commands)
        logging.info("Set GDAC to %d (VthinAltCoarse / VthinAltFine = %d / %d)" % (value, self.register.get_global_register_value("Vthin_AltCoarse"), self.register.get_global_register_value("Vthin_AltFine")))

    def read_pixel_register(self, pix_regs=["EnableDigInj", "Imon", "Enable", "C_High", "C_Low", "TDAC", "FDAC"], dcs=range(40), overwrite_config=False):
        '''Reads the pixel register, interprets the data and returns a masked numpy arrays with the data for the chosen pixel register.
        Pixels without any data are masked.

        Parameters
        ----------
        pix_regs : iterable, int
            List of pixel register to read (e.g. enable, c_high, ...).
        dcs : iterable, int
            List of double columns to read.
        overwrite_config : bool
            The read values overwrite the config in RAM if true.

        Returns
        -------
        list of masked numpy.ndarrays
        '''
        result = []
        for pix_reg in pix_regs:
            pixel_data = np.ma.masked_array(np.zeros(shape=(80, 336), dtype=np.uint32), mask=True)  # the result pixel array, only pixel with data are not masked
            for dc in dcs:
                self.send_commands(self.register.get_commands("rdfrontend", name=[pix_reg], dcs=[dc]))
                data = self.readout.read_data()
                interpret_pixel_data(data, dc, pixel_data, invert=False if pix_reg.lower() == "enablediginj" else True)
            if overwrite_config:
                self.register.set_pixel_register(pix_reg, pixel_data.data)
            result.append(pixel_data)
        return result


def invert_pixel_mask(mask):
    '''Invert pixel mask (0->1, 1(and greater)->0).

    Parameters
    ----------
    mask : array-like
        Mask.

    Returns
    -------
    inverted_mask : array-like
        Inverted Mask.
    '''
    inverted_mask = np.ones(shape=(80, 336), dtype=np.dtype('>u1'))
    inverted_mask[mask >= 1] = 0
    return inverted_mask


def make_pixel_mask(steps, shift, default=0, value=1, enable_columns=None, mask=None):
    '''Generate pixel mask.

    Parameters
    ----------
    steps : int
        Number of mask steps, e.g. steps=3 (every third pixel is enabled), steps=336 (one pixel per column), steps=672 (one pixel per double column).
    shift : int
        Shift mask by given value to the bottom (towards higher row numbers). From 0 to (steps - 1).
    default : int
        Value of pixels that are not selected by the mask.
    value : int
        Value of pixels that are selected by the mask.
    enable_columns : list
        List of columns where the shift mask will be applied. List elements can range from 1 to 80.
    mask : array_like
        Additional mask. Must be convertible to an array of booleans with the same shape as mask array. True indicates a masked (i.e. invalid) data. Masked pixels will be set to default value.

    Returns
    -------
    mask_array : numpy.ndarray
        Mask array.

    Usage
    -----
    shift_mask = 'enable'
    steps = 3 # three step mask
    for mask_step in range(steps):
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        mask_array = make_pixel_mask(steps=steps, step=mask_step)
        self.register.set_pixel_register_value(shift_mask, mask_array)
        commands.extend(self.register.get_commands("wrfrontend", same_mask_for_all_dc=True, name=shift_mask))
        self.register_utils.send_commands(commands)
        # do something here
    '''
    dimension = (80, 336)
    # value = np.zeros(dimension, dtype = np.uint8)
    mask_array = np.empty(dimension, dtype=np.uint8)
    mask_array.fill(default)
    # FE columns and rows are starting from 1
    if enable_columns:
        odd_columns = [odd - 1 for odd in enable_columns if odd % 2 != 0]
        even_columns = [even - 1 for even in enable_columns if even % 2 == 0]
    else:
        odd_columns = range(0, 80, 2)
        even_columns = range(1, 80, 2)
    odd_rows = np.arange(shift % steps, 336, steps)
    even_row_offset = ((steps // 2) + shift) % steps  # // integer devision
    even_rows = np.arange(even_row_offset, 336, steps)
    if odd_columns:
        odd_col_row = cartesian((odd_columns, odd_rows))  # get any combination of column and row, no for loop needed
        mask_array[odd_col_row[:, 0], odd_col_row[:, 1]] = value  # advanced indexing
    if even_columns:
        even_col_row = cartesian((even_columns, even_rows))
        mask_array[even_col_row[:, 0], even_col_row[:, 1]] = value
    if mask is not None:
        mask_array = np.ma.array(mask_array, mask=mask, fill_value=default)
        mask_array = mask_array.filled()
    return mask_array


def make_pixel_mask_from_col_row(column, row, default=0, value=1):
    '''Generate mask from column and row lists

    Parameters
    ----------
    column : iterable, int
        List of colums values.
    row : iterable, int
        List of row values.
    default : int
        Value of pixels that are not selected by the mask.
    value : int
        Value of pixels that are selected by the mask.

    Returns
    -------
    mask : numpy.ndarray
    '''
    # FE columns and rows start from 1
    col_array = np.array(column) - 1
    row_array = np.array(row) - 1
    if np.any(col_array >= 80) or np.any(col_array < 0) or np.any(row_array >= 336) or np.any(col_array < 0):
        raise ValueError('Column and/or row out of range')
    dimension = (80, 336)
    # value = np.zeros(dimension, dtype = np.uint8)
    mask = np.empty(dimension, dtype=np.uint8)
    mask.fill(default)
    mask[col_array, row_array] = value  # advanced indexing
    return mask


def make_box_pixel_mask_from_col_row(column, row, default=0, value=1):
    '''Generate box shaped mask from column and row lists. Takes the minimum and maximum value from each list.

    Parameters
    ----------
    column : iterable, int
        List of colums values.
    row : iterable, int
        List of row values.
    default : int
        Value of pixels that are not selected by the mask.
    value : int
        Value of pixels that are selected by the mask.

    Returns
    -------
    numpy.ndarray
    '''
    # FE columns and rows start from 1
    col_array = np.array(column) - 1
    row_array = np.array(row) - 1
    if np.any(col_array >= 80) or np.any(col_array < 0) or np.any(row_array >= 336) or np.any(col_array < 0):
        raise ValueError('Column and/or row out of range')
    dimension = (80, 336)
    # value = np.zeros(dimension, dtype = np.uint8)
    mask = np.empty(dimension, dtype=np.uint8)
    mask.fill(default)
    if column and row:
        mask[col_array.min():col_array.max() + 1, row_array.min():row_array.max() + 1] = value  # advanced indexing
    return mask


def cartesian(arrays, out=None):
    """
    Generate a cartesian product of input arrays.
    Similar to itertools.combinations().

    Parameters
    ----------
    arrays : list of array-like
        1-D arrays to form the cartesian product of.
    out : ndarray
        Array to place the cartesian product in.

    Returns
    -------
    out : ndarray
        2-D array of shape (M, len(arrays)) containing cartesian products
        formed of input arrays.

    Examples
    --------
    >>> cartesian(([1, 2, 3], [4, 5], [6, 7]))
    array([[1, 4, 6],
           [1, 4, 7],
           [1, 5, 6],
           [1, 5, 7],
           [2, 4, 6],
           [2, 4, 7],
           [2, 5, 6],
           [2, 5, 7],
           [3, 4, 6],
           [3, 4, 7],
           [3, 5, 6],
           [3, 5, 7]])

    Note
    ----
    http://stackoverflow.com/questions/1208118/using-numpy-to-build-an-array-of-all-combinations-of-two-arrays

    """

    arrays = [np.asarray(x) for x in arrays]
    dtype = arrays[0].dtype

    n = np.prod([x.size for x in arrays])
    if out is None:
        out = np.zeros([n, len(arrays)], dtype=dtype)

    m = n / arrays[0].size
    out[:, 0] = np.repeat(arrays[0], m)
    if arrays[1:]:
        cartesian(arrays[1:], out=out[0:m, 1:])
        for j in xrange(1, arrays[0].size):
            out[j * m:(j + 1) * m, 1:] = out[0:m, 1:]
    return out


def parse_key_value(filename, key, deletechars=''):
    with open(filename, 'r') as f:
        return parse_key_value_from_file(f, key, deletechars)


def parse_key_value_from_file(f, key, deletechars=''):
    for line in f.readlines():
        key_value = re.split("\s+|[\s]*=[\s]*", line)
        if (key_value[0].translate(None, deletechars).lower() == key.translate(None, deletechars).lower()):
            if len(key_value) > 1:
                return key_value[0].translate(None, deletechars).lower(), key_value[1].translate(None, deletechars).lower()
            else:
                raise ValueError('Value not found')
        else:
            return None