# from analysis.plotting.plotting import plot_occupancy, make_occupancy_hist
from analysis.analyze_raw_data import AnalyzeRawData
from fei4.register_utils import invert_pixel_mask
from scan.scan import ScanBase
from scan.scan_utils import scan_loop
from scan.run_manager import RunManager
# import logging


class AnalogScan(ScanBase):
    _scan_id = "analog_scan"

    _default_scan_configuration = {
        "mask_steps": 3,
        "repeat_command": 100,
        "scan_parameters": {'PlsrDAC': 200},
        "enable_tdc": False,
        "use_enable_mask": False
    }

    def activate_tdc(self):
        self.dut['tdc_rx2']['ENABLE'] = True

    def deactivate_tdc(self):
        self.dut['tdc_rx2']['ENABLE'] = False

    def configure(self):
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        self.register.set_global_register_value('PlsrDAC', self.scan_parameters.PlsrDAC)
        commands.extend(self.register.get_commands("wrregister", name=['PlsrDAC']))
        self.register_utils.send_commands(commands)

    def scan(self):
        '''Scan loop

        Parameters
        ----------
        mask_steps : int
            Number of mask steps.
        repeat_command : int
            Number of injections.
        scan_parameter : string
            Name of global register.
        scan_parameter_value : int
            Specify scan steps. These values will be written into global register scan_parameter.
        enable_tdc : bool
            Enables TDC.
        use_enable_mask : bool
            Use enable mask for masking pixels.

        Note
        ----
        This scan is very similar to the threshold scan.
        This scan can also be used for ToT verification: change scan_parameter_value to desired injection charge (in units of PulsrDAC).
        '''
        with self.readout():
            cal_lvl1_command = self.register.get_commands("cal")[0] + self.register.get_commands("zeros", length=40)[0] + self.register.get_commands("lv1")[0]

            if self.enable_tdc:
                # activate TDC arming
                self.dut['tdc_rx2']['EN_ARMING'] = True
                scan_loop(self, cal_lvl1_command, repeat_command=self.repeat_command, use_delay=True, mask_steps=self.mask_steps, enable_mask_steps=None, enable_double_columns=None, same_mask_for_all_dc=True, bol_function=self.activate_tdc, eol_function=self.deactivate_tdc, digital_injection=False, enable_shift_masks=["Enable", "C_Low", "C_High"], restore_shift_masks=False, mask=invert_pixel_mask(self.register.get_pixel_register_value('Enable')) if self.use_enable_mask else None)
            else:
                scan_loop(self, cal_lvl1_command, repeat_command=self.repeat_command, use_delay=True, mask_steps=self.mask_steps, enable_mask_steps=None, enable_double_columns=None, same_mask_for_all_dc=True, digital_injection=False, enable_shift_masks=["Enable", "C_Low", "C_High"], restore_shift_masks=False, mask=invert_pixel_mask(self.register.get_pixel_register_value('Enable')) if self.use_enable_mask else None)

        # plotting data
#         plot_occupancy(hist=make_occupancy_hist(*convert_data_array(data_array_from_data_dict_iterable(self.readout.data), filter_func=is_data_record, converter_func=get_col_row_array_from_data_record_array)), z_max='median', filename=self.scan_data_filename + "_occupancy.pdf")

    def analyze(self):
        with AnalyzeRawData(raw_data_file=self.output_filename, create_pdf=True) as analyze_raw_data:
            analyze_raw_data.create_tot_hist = True
            if self.enable_tdc:
                analyze_raw_data.create_tdc_counter_hist = True  # histogram all TDC words
                analyze_raw_data.create_tdc_hist = True  # histogram the hit TDC information
                analyze_raw_data.interpreter.use_tdc_word(True)  # align events at the TDC word
            analyze_raw_data.interpret_word_table()
            analyze_raw_data.plot_histograms()

if __name__ == "__main__":
    wait = RunManager.run_run(AnalogScan, 'configuration.yaml')
    wait()