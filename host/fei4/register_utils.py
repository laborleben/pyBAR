import struct
import array
import math
import time
import numpy as np
import re

from utils.utils import bitarray_to_array


class FEI4RegisterUtils(object):
    def __init__(self, device, readout, register):
        self.device = device
        self.readout = readout
        self.register = register
        self.command_memory_byte_offset = 8
        self.command_memory_byte_size = 2048 - self.command_memory_byte_offset  # 16 bytes of register data

    def send_commands(self, commands, repeat=1, wait_for_finish=True, concatenate=False, clear_memory=False):
        if concatenate:
            command = reduce(lambda x, y: x + y, commands)
            self.send_command(command=command, repeat=repeat, wait_for_finish=wait_for_finish, set_length=True, clear_memory=True)
        else:
            max_length = 0
            self.set_hardware_repeat(1)
            for command in commands:
                max_length = max(command.length(), max_length)
                self.send_command(command=command, repeat=None, wait_for_finish=wait_for_finish, set_length=True, clear_memory=False)
            if clear_memory:
                self.clear_command_memory(length=max_length)

    def send_command(self, command, repeat=1, wait_for_finish=True, set_length=True, clear_memory=False):
        if repeat is not None:
            self.set_hardware_repeat(repeat)
        # write command into memory
        command_length = self.set_command(command, set_length=set_length)
        # sending command
        self.start_command()
        # wait for command to be finished
        if wait_for_finish:
            self.wait_for_command(length=command_length, repeat=repeat)
        # clear command memory
        if clear_memory:
            self.clear_command_memory(length=command_length)

    def clear_command_memory(self, length=None):
        self.set_command(self.register.get_commands("zeros", length=(self.self.command_memory_byte_size * 8) if length is None else length)[0], set_length=False)

    def set_command_length(self, lenght):
        bit_length_array = array.array('B', struct.pack('H', lenght))
        self.device.WriteExternal(address=0 + 3, data=bit_length_array)

    def set_command(self, command, set_length=True, byte_offset=0):
        command_length = command.length()
        # set command bit length
        if set_length:
            self.set_command_length(command_length)
        # set command
        data = bitarray_to_array(command)
        if self.command_memory_byte_size < len(data) + byte_offset:
            raise ValueError('Length of command or offset is too big')
        self.device.WriteExternal(address=0 + self.command_memory_byte_offset + byte_offset, data=data)
        return command_length

    def start_command(self):
        self.device.WriteExternal(address=0 + 1, data=(0, ))

    def set_hardware_repeat(self, repeat=1):
        repeat_array = array.array('B', struct.pack('H', repeat))
        self.device.WriteExternal(address=0 + 5, data=repeat_array)

    def wait_for_command(self, length=None, repeat=None):

        # print self.device.ReadExternal(address = 0+1, size = 1)[0]
        if length is not None:
            if repeat is None:
                repeat = 1
            # print 'sleeping'
            time.sleep((length + 500) * 0.000000025 * repeat)  # TODO: optimize wait time
        while not self.is_ready:
            pass

    @property
    def is_ready(self):
        return (self.device.ReadExternal(address=0 + 1, size=1)[0] & 0x01) == 1

    def global_reset(self):
        '''FEI4 Global Reset

        Special function to do a global reset on FEI4. Sequence of commands has to be like this, otherwise FEI4B will be left in weird state.
        '''
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        # vthin_altfine, vthin_altcoarse = self.register.get_global_register_value("Vthin_AltFine"), self.register.get_global_register_value("Vthin_AltCoarse")
        # self.register.set_global_register_value("Vthin_AltFine", 255)
        # self.register.set_global_register_value("Vthin_AltCoarse", 255)
        # commands.extend(self.register.get_commands("wrregister", name = ["Vthin_AltFine", "Vthin_AltCoarse"]))
        commands.extend(self.register.get_commands("globalreset"))
        self.send_commands(commands)
        time.sleep(0.1)
        commands[:] = []
        commands.extend(self.register.get_commands("confmode"))
        # self.register.set_global_register_value("Vthin_AltFine", vthin_altfine)
        # self.register.set_global_register_value("Vthin_AltCoarse", vthin_altcoarse)
        # commands.extend(self.register.get_commands("wrregister", name = ["Vthin_AltFine", "Vthin_AltCoarse"]))
        commands.extend(self.register.get_commands("runmode"))
        self.send_commands(commands)

    def configure_all(self, same_mask_for_all_dc=False, do_global_rest=False):
        if do_global_rest:
            self.global_reset()
        self.configure_global()
        self.configure_pixel(same_mask_for_all_dc=same_mask_for_all_dc)

    def configure_global(self):
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        commands.extend(self.register.get_commands("wrregister", readonly=False))
        commands.extend(self.register.get_commands("runmode"))
        self.send_commands(commands)

    def configure_pixel(self, same_mask_for_all_dc=False):
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        commands.extend(self.register.get_commands("wrfrontend", same_mask_for_all_dc=same_mask_for_all_dc, name=["Imon", "Enable", "c_high", "c_low", "TDAC", "FDAC"]))
        commands.extend(self.register.get_commands("wrfrontend", same_mask_for_all_dc=same_mask_for_all_dc, name=["EnableDigInj"]))  # write EnableDigInj last
        commands.extend(self.register.get_commands("runmode"))
        self.send_commands(commands)

    def read_service_records(self):
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        self.send_commands(commands)
        self.readout.reset_sram_fifo()
        commands = []
        self.register.set_global_register_value('ReadErrorReq', 1)
        commands.extend(self.register.get_commands("wrregister", name=['ReadErrorReq']))
        commands.extend(self.register.get_commands("globalpulse", width=0))
        self.register.set_global_register_value('ReadErrorReq', 0)
        commands.extend(self.register.get_commands("wrregister", name=['ReadErrorReq']))
        self.send_commands(commands)

        data = self.readout.read_data()

        commands = []
        commands.extend(self.register.get_commands("runmode"))
        self.send_commands(commands)
        return data

    def make_pixel_mask(self, steps, shift, default=0, value=1, mask=None):
        '''Generate pixel mask.

        Parameters
        ----------
        steps : int
            Number of mask steps. E.g. steps=3 means every third pixel is enabled.
        shift : int
            Shift mask by given value to the bottom (towards higher row numbers). From 0 to steps - 1.
        default : int
            Value of pixels that are not selected by the mask.
        value : int
            Value of pixels that are selected by the mask.
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
            mask_array = self.register_utils.make_pixel_mask(steps=steps, step=mask_step)
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
        odd_columns = np.arange(0, 80, 2)
        even_columns = np.arange(1, 80, 2)
        odd_rows = np.arange((0 + shift) % steps, 336, steps)
        even_row_offset = (int(math.floor(steps / 2) + shift)) % steps
        even_rows = np.arange(0 + even_row_offset, 336, steps)
        odd_col_row = cartesian((odd_columns, odd_rows))  # get any combination of column and row, no for loop needed
        even_col_row = cartesian((even_columns, even_rows))
        mask_array[odd_col_row[:, 0], odd_col_row[:, 1]] = value  # advanced indexing
        mask_array[even_col_row[:, 0], even_col_row[:, 1]] = value
        if mask is not None:
            mask_array = np.ma.array(mask_array, mask=mask, fill_value=default)
            mask_array = mask_array.filled()
        return mask_array

    def make_pixel_mask_from_col_row(self, column, row, default=0, value=1):
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

    def make_box_pixel_mask_from_col_row(self, column, row, default=0, value=1):
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
                print key_value
                print len(key_value)
                return key_value[0].translate(None, deletechars).lower(), key_value[1].translate(None, deletechars).lower()
            else:
                raise ValueError('Value not found')
        else:
            return None
