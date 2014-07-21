""" Script to tune the feedback current to the charge@ToT. Charge in PlsrDAC. Binary search algorithm. Bit 0 is always scanned twice with value 1 and 0.
    Only the pixels used in the analog injection are taken into account.
"""
import numpy as np
import logging

from daq.readout import open_raw_data_file, get_tot_array_from_data_record_array, convert_data_array, data_array_from_data_dict_iterable, is_data_record, is_data_from_channel, logical_and
from analysis.plotting.plotting import plot_tot
from scan.scan import ScanBase

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - [%(levelname)-8s] (%(threadName)-10s) %(message)s")


local_configuration = {
    "target_charge": 280,
    "target_tot": 5,
    "feedback_tune_bits": range(7, -1, -1),
    "n_injections": 50,
    "abort_precision_tot": 0.1,
    "plot_intermediate_steps": False,
    "plots_filename": None
}


class FeedbackTune(ScanBase):
    scan_id = "feedback_tune"

    def set_target_charge(self, plsr_dac=250):
        self.target_charge = plsr_dac

    def write_target_charge(self):
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        self.register.set_global_register_value("PlsrDAC", self.target_charge)
        commands.extend(self.register.get_commands("wrregister", name="PlsrDAC"))
        self.register_utils.send_commands(commands)

    def set_target_tot(self, Tot=5):
        self.TargetTot = Tot

    def set_abort_precision(self, delta_tot=0.1):
        self.abort_precision = delta_tot

    def set_prmp_vbpf_bit(self, bit_position, bit_value=1):
        commands = []
        commands.extend(self.register.get_commands("confmode"))
        if(bit_value == 1):
            self.register.set_global_register_value("PrmpVbpf", self.register.get_global_register_value("PrmpVbpf") | (1 << bit_position))
        else:
            self.register.set_global_register_value("PrmpVbpf", self.register.get_global_register_value("PrmpVbpf") & ~(1 << bit_position))
        commands.extend(self.register.get_commands("wrregister", name=["PrmpVbpf"]))
        self.register_utils.send_commands(commands)

    def set_feedback_tune_bits(self, FeedbackTuneBits=range(7, -1, -1)):
        self.FeedbackTuneBits = FeedbackTuneBits

    def set_n_injections(self, Ninjections=100):
        self.Ninjections = Ninjections

    def scan(self, target_tot, target_charge, feedback_tune_bits=range(7, -1, -1), abort_precision_tot=0.1, n_injections=50, plots_filename=None, plot_intermediate_steps=False, **kwargs):
        #  set scan settings
        self.set_n_injections(n_injections)
        self.set_target_charge(target_charge)
        self.set_target_tot(target_tot)
        self.set_abort_precision(abort_precision_tot)
        self.set_feedback_tune_bits(feedback_tune_bits)

        self.write_target_charge()

        for PrmpVbpf_bit in self.FeedbackTuneBits:  # reset all GDAC bits
            self.set_prmp_vbpf_bit(PrmpVbpf_bit, bit_value=0)

        addedAdditionalLastBitScan = False
        lastBitResult = self.Ninjections

        mask_steps = 3
        enable_mask_steps = [0]  # one mask step to increase speed, no effect on precision

        scan_parameter = 'PrmpVbpf'

        with open_raw_data_file(filename=self.scan_data_filename, title=self.scan_id, scan_parameters=[scan_parameter]) as raw_data_file:
            tot_mean_best = 0
            prmp_vbpf_best = self.register.get_global_register_value("PrmpVbpf")
            for PrmpVbpf_bit in self.FeedbackTuneBits:
                if(not addedAdditionalLastBitScan):
                    self.set_prmp_vbpf_bit(PrmpVbpf_bit)
                    logging.info('PrmpVbpf setting: %d, bit %d = 1' % (self.register.get_global_register_value("PrmpVbpf"), PrmpVbpf_bit))
                else:
                    self.set_prmp_vbpf_bit(PrmpVbpf_bit, bit_value=0)
                    logging.info('PrmpVbpf setting: %d, bit %d = 0' % (self.register.get_global_register_value("PrmpVbpf"), PrmpVbpf_bit))

                scan_parameter_value = self.register.get_global_register_value("PrmpVbpf")

                self.readout.start()
                repeat_command = self.Ninjections

                cal_lvl1_command = self.register.get_commands("cal")[0] + self.register.get_commands("zeros", length=40)[0] + self.register.get_commands("lv1")[0] + self.register.get_commands("zeros", mask_steps=mask_steps)[0]
                self.scan_loop(cal_lvl1_command, repeat_command=repeat_command, hardware_repeat=True, mask_steps=mask_steps, enable_mask_steps=enable_mask_steps, enable_double_columns=None, same_mask_for_all_dc=True, eol_function=None, digital_injection=False, enable_shift_masks=["Enable", "C_High", "C_Low"], restore_shift_masks=True, mask=None)

                self.readout.stop()
                raw_data_file.append(self.readout.data, scan_parameters={scan_parameter: scan_parameter_value})

                tots = get_tot_array_from_data_record_array(convert_data_array(data_array_from_data_dict_iterable(self.readout.data), filter_func=logical_and(is_data_record, is_data_from_channel(4))))
                mean_tot = np.mean(tots)
                if np.isnan(mean_tot):
                    logging.error("No hits, ToT calculation not possible, tuning will fail")

                if abs(mean_tot - self.TargetTot) < abs(tot_mean_best - self.TargetTot):
                    tot_mean_best = mean_tot
                    prmp_vbpf_best = self.register.get_global_register_value("PrmpVbpf")

                logging.info('Mean ToT = %f' % mean_tot)
                TotArray, _ = np.histogram(a=tots, range=(0, 16), bins=16)
                if plot_intermediate_steps:
                    plot_tot(hist=TotArray, title='Time-over-threshold distribution (PrmpVbpf ' + str(scan_parameter_value) + ')', filename=plots_filename)

                if(abs(mean_tot - self.TargetTot) < self.abort_precision and PrmpVbpf_bit > 0):  # abort if good value already found to save time
                    logging.info('Good result already achieved, skipping missing bits')
                    break

                if(PrmpVbpf_bit > 0 and mean_tot < self.TargetTot):
                    self.set_prmp_vbpf_bit(PrmpVbpf_bit, bit_value=0)
                    logging.info('Mean ToT = %f < %d ToT, set bit %d = 0' % (mean_tot, self.TargetTot, PrmpVbpf_bit))

                if(PrmpVbpf_bit == 0):
                    if not(addedAdditionalLastBitScan):  # scan bit = 0 with the correct value again
                        addedAdditionalLastBitScan = True
                        lastBitResult = mean_tot
                        self.FeedbackTuneBits.append(0)  # bit 0 has to be scanned twice
                    else:
                        logging.info('Scanned bit 0 = 0 with %f instead of %f for scanned bit 0 = 1' % (mean_tot, lastBitResult))
                        if(abs(mean_tot - self.TargetTot) > abs(lastBitResult - self.TargetTot)):  # if bit 0 = 0 is worse than bit 0 = 1, so go back
                            self.set_prmp_vbpf_bit(PrmpVbpf_bit, bit_value=1)
                            mean_tot = lastBitResult
                            logging.info('Set bit 0 = 1')
                        else:
                            logging.info('Set bit 0 = 0')
                    if abs(mean_tot - self.TargetTot) > abs(tot_mean_best - self.TargetTot):
                            logging.info("Binary search converged to non optimal value, take best measured value instead")
                            mean_tot = tot_mean_best
                            self.register.set_global_register_value("PrmpVbpf", prmp_vbpf_best)

            if self.register.get_global_register_value("PrmpVbpf") == 0 or self.register.get_global_register_value("PrmpVbpf") == 254:
                logging.warning('PrmpVbpf reached minimum/maximum value')

            if(abs(mean_tot - self.TargetTot) > 2 * self.abort_precision):
                logging.warning('Tuning of PrmpVbpf to %d ToT failed. Difference = %f ToT. PrmpVbpf = %d' % (self.TargetTot, abs(mean_tot - self.TargetTot), self.register.get_global_register_value("PrmpVbpf")))
            else:
                logging.info('Tuned PrmpVbpf to %d' % self.register.get_global_register_value("PrmpVbpf"))

            self.result = TotArray
            plot_tot(hist=TotArray, title='Time-over-threshold distribution after feedback tuning (PrmpVbpf %d)' % scan_parameter_value, filename=plots_filename)

if __name__ == "__main__":
    import configuration
    scan = FeedbackTune(**configuration.default_configuration)
    scan.start(use_thread=False, **local_configuration)
    scan.stop()
    scan.register.save_configuration(scan.device_configuration['configuration_file'])
