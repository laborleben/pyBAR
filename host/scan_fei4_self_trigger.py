import time
import logging
import math
import numpy as np

from scan.scan import ScanBase
from daq.readout import open_raw_data_file
from fei4.register_utils import make_box_pixel_mask_from_col_row, invert_pixel_mask

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] (%(threadName)-10s) %(message)s")


local_configuration = {
    "col_span": [1, 80],
    "row_span": [1, 336],
    "overwrite_mask": False,
    "use_enable_mask": True,
    "timeout_no_data": 10,
    "scan_timeout": 1 * 60,
    "trig_latency": 239,
    "trig_count": 4
}


class FEI4SelfTriggerScan(ScanBase):
    scan_id = "fei4_self_trigger_scan"

    def scan(self):
        '''Scan loop

        Parameters
        ----------
        col_span : list, tuple
            Column range (from minimum to maximum value). From 1 to 80.
        row_span : list, tuple
            Row range (from minimum to maximum value). From 1 to 336.
        timeout_no_data : int
            In seconds; if no data, stop scan after given time.
        scan_timeout : int
            In seconds; stop scan after given time.
        trig_latency : int
            FE global register Trig_Lat.
        trig_count : int
            FE global register Trig_Count.
        '''
        with open_raw_data_file(filename=self.scan_data_filename, title=self.scan_id) as raw_data_file:
            self.readout.start()
            self.set_self_trigger(True)
            wait_for_first_data = True
            show_trigger_message_at = 10 ** (int(math.floor(math.log10(self.scan_timeout) - math.log10(3) / math.log10(10))))
            time_current_iteration = time.time()
            saw_no_data_at_time = time_current_iteration
            saw_data_at_time = time_current_iteration
            scan_start_time = time_current_iteration
            no_data_at_time = time_current_iteration
            time_from_last_iteration = 0
            scan_stop_time = scan_start_time + self.scan_timeout
            while not self.stop_thread_event.wait(self.readout.readout_interval):
                time_last_iteration = time_current_iteration
                time_current_iteration = time.time()
                time_from_last_iteration = time_current_iteration - time_last_iteration
                if ((time_current_iteration - scan_start_time) % show_trigger_message_at < (time_last_iteration - scan_start_time) % show_trigger_message_at):
                    logging.info('Scan runtime: %d seconds', time_current_iteration - scan_start_time)
                    if not any(self.readout.get_rx_sync_status()):
                        self.stop_thread_event.set()
                        logging.error('No RX sync. Stopping Scan...')
                    if any(self.readout.get_rx_8b10b_error_count()):
                        self.stop_thread_event.set()
                        logging.error('RX 8b10b error(s) detected. Stopping Scan...')
                    if any(self.readout.get_rx_fifo_discard_count()):
                        self.stop_thread_event.set()
                        logging.error('RX FIFO discard error(s) detected. Stopping Scan...')
                if self.scan_timeout is not None and time_current_iteration > scan_stop_time:
                    logging.info('Reached maximum scan time. Stopping Scan...')
                    self.stop_thread_event.set()
                try:
                    raw_data_file.append((self.readout.data.popleft(),))
                except IndexError:  # no data
                    no_data_at_time = time_current_iteration
                    if self.timeout_no_data is not None and wait_for_first_data is False and saw_no_data_at_time > (saw_data_at_time + self.timeout_no_data):
                        logging.info('Reached no data timeout. Stopping Scan...')
                        self.stop_thread_event.set()
                    elif wait_for_first_data is False:
                        saw_no_data_at_time = no_data_at_time

                    if no_data_at_time > (saw_data_at_time + 10):
                        scan_stop_time += time_from_last_iteration
                else:
                    saw_data_at_time = time_current_iteration

                    if wait_for_first_data is True:
                        logging.info('Taking data...')
                        wait_for_first_data = False

            self.set_self_trigger(False)
            self.readout.stop()

            raw_data_file.append(self.readout.data)

    def configure(self):
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        pixel_reg = 'Enable'  # enabled pixels set to 1
        mask = make_box_pixel_mask_from_col_row(column=self.col_span, row=self.row_span)  # 1 for selected columns, else 0
        if self.overwrite_mask:
            pixel_mask = mask
        else:
            pixel_mask = np.logical_and(mask, self.register.get_pixel_register_value(pixel_reg))
        self.register.set_pixel_register_value(pixel_reg, pixel_mask)
        commands.extend(self.register.get_commands("wrfrontend", same_mask_for_all_dc=False, name=pixel_reg))
        pixel_reg = 'Imon'  # disabled pixels set to 1
        if self.use_enable_mask:
            self.register.set_pixel_register_value(pixel_reg, invert_pixel_mask(self.register.get_pixel_register_value('Enable')))
        else:
            self.register.set_pixel_register_value(pixel_reg, 0)
        mask = make_box_pixel_mask_from_col_row(column=self.col_span, row=self.row_span, default=1, value=0)  # 0 for selected columns, else 1
        if self.overwrite_mask:
            pixel_mask = mask
        else:
            pixel_mask = np.logical_or(mask, self.register.get_pixel_register_value(pixel_reg))
        self.register.set_pixel_register_value(pixel_reg, pixel_mask)
        commands.extend(self.register.get_commands("wrfrontend", same_mask_for_all_dc=False, name=pixel_reg))
        # disable C_inj mask
        pixel_reg = "C_High"
        self.register.set_pixel_register_value(pixel_reg, 0)
        commands.extend(self.register.get_commands("wrfrontend", same_mask_for_all_dc=True, name=pixel_reg))
        pixel_reg = "C_Low"
        self.register.set_pixel_register_value(pixel_reg, 0)
        commands.extend(self.register.get_commands("wrfrontend", same_mask_for_all_dc=True, name=pixel_reg))
        # enable GateHitOr that enables FE self-trigger mode
        self.register.set_global_register_value("Trig_Lat", self.trig_latency)  # set trigger latency, this latency sets the hits at the first relative BCID bins
        self.register.set_global_register_value("Trig_Count", self.trig_count)  # set number of consecutive triggers
        commands.extend(self.register.get_commands("wrregister", name=["Trig_Lat", "Trig_Count"]))
        # send commands
        self.register_utils.send_commands(commands)

    def set_self_trigger(self, enable=True):
        logging.info('%s FEI4 self-trigger' % ('Enable' if enable is True else "Disable"))
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        self.register.set_global_register_value("GateHitOr", 1 if enable else 0)  # enable FE self-trigger mode
        commands.extend(self.register.get_commands("wrregister", name=["GateHitOr"]))
        if enable:
            commands.extend(self.register.get_commands("runmode"))
        self.register_utils.send_commands(commands)

    def analyze(self):
        from analysis.analyze_raw_data import AnalyzeRawData
        output_file = self.scan_data_filename + "_interpreted.h5"
        with AnalyzeRawData(raw_data_file=self.scan_data_filename + ".h5", analyzed_data_file=output_file) as analyze_raw_data:
            analyze_raw_data.create_cluster_size_hist = True  # can be set to false to omit cluster hit creation, can save some time, standard setting is false
            analyze_raw_data.create_source_scan_hist = True
            analyze_raw_data.create_cluster_tot_hist = True
            analyze_raw_data.interpreter.set_warning_output(False)
            analyze_raw_data.clusterizer.set_warning_output(False)
            analyze_raw_data.interpret_word_table()
            analyze_raw_data.interpreter.print_summary()
            analyze_raw_data.plot_histograms(scan_data_filename=self.scan_data_filename)

if __name__ == "__main__":
    import configuration
    scan = FEI4SelfTriggerScan(**configuration.default_configuration)
    scan.start(run_configure=True, run_analyze=True, use_thread=True, **local_configuration)
    scan.stop()