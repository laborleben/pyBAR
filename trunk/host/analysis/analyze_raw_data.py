''' Script to convert the raw data and to plot all histograms'''
import tables as tb
from tables import dtype_from_descr, Col
import numpy as np
import logging
import os
from scipy.optimize import curve_fit
from scipy.special import erf
import multiprocessing as mp
from functools import partial

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - [%(levelname)-8s] (%(threadName)-10s) %(message)s")

from RawDataConverter import data_struct
from plotting import plotting
from matplotlib.backends.backend_pdf import PdfPages
from RawDataConverter.data_interpreter import PyDataInterpreter
from RawDataConverter.data_histograming import PyDataHistograming
from RawDataConverter.data_clusterizer import PyDataClusterizer


def scurve(x, A, mu, sigma):
    return 0.5 * A * erf((x - mu) / (np.sqrt(2) * sigma)) + 0.5 * A


def fit_scurve(scurve_data, PlsrDAC):  # data of some pixels to fit, has to be global for the multiprocessing module
    index = np.argmax(np.diff(scurve_data))
    max_occ = np.median(scurve_data[index:])
    threshold = PlsrDAC[index]
    if abs(max_occ) <= 1e-08:  # or index == 0: occupancy is zero or close to zero
        popt = [0, 0, 0]
    else:
        try:
            popt, _ = curve_fit(scurve, PlsrDAC, scurve_data, p0=[max_occ, threshold, 2.5])
        except RuntimeError:  # fit failed
            popt = [0, 0, 0]
    return popt[1:3]


def fit_scurves_subset(hist, PlsrDAC):
    '''
    Fits S-curve for each pixel

    Parameters
    ----------
    hist : array like, shape = (number of pixel, PlsrDAC range)
        Array of input y data.
    PlsrDAC : array-like
        Input x data.

    Returns
    -------
    list with fit result tuples (amplitude, mu, sigma)
    '''
    result = []
    n_failed_pxel_fits = 0
    for iPixel in range(0, hist.shape[0]):
        try:
            popt, _ = curve_fit(scurve, PlsrDAC, hist[iPixel], p0=[100, 50, 3])
        except RuntimeError:
            popt = [0, 0, 0]
            n_failed_pxel_fits = n_failed_pxel_fits + 1
        result.append(popt[1:3])
        if(iPixel % 2000 == 0):
            logging.info('Fitting S-curve: %d%%' % (iPixel * 100. / 26880.))
    logging.info('Fitting S-curve: 100%')
    logging.info('S-curve fit failed for %d pixel' % n_failed_pxel_fits)
    return result


def generate_threshold_mask(hist):
    '''Masking array elements when equal 0.0 or greater than 2*median

    Parameters
    ----------
    hist : array_like
        Input data.

    Returns
    -------
    masked array
        Returns copy of the array with masked elements.
    '''
    masked_array = np.ma.masked_values(hist, 0)
    masked_array = np.ma.masked_greater(masked_array, 10 * np.ma.median(hist))
#     masked_array = np.ma.array(hist, mask=((hist < 0.1) | (hist > 10 * np.median(hist))))
#     logging.info('Masking %d pixel(s)' % np.ma.count_masked(masked_array))
    return np.ma.getmaskarray(masked_array)


class AnalyzeRawData(object):
    """A class to analyze FE-I4 raw data"""
    def __init__(self, raw_data_file=None, analyzed_data_file=None):
        self.interpreter = PyDataInterpreter()
        self.histograming = PyDataHistograming()
        self.clusterizer = PyDataClusterizer()
        self._raw_data_file = raw_data_file
        self._analyzed_data_file = analyzed_data_file
        self.set_standard_settings()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        del self.interpreter
        del self.histograming
        del self.clusterizer

    def set_standard_settings(self):
        self.out_file_h5 = None
        self.meta_event_index = None
        self._chunk_size = 2000000
        self._filter_table = tb.Filters(complib='blosc', complevel=5, fletcher32=False)
        self.FEI4B = False
        self.create_hit_table = False
        self.create_meta_event_index = True
        self.create_meta_word_index = False
        self.create_occupancy_hist = True
        self.create_source_scan_hist = False
        self.create_tot_hist = True
        self.create_rel_bcid_hist = True
        self.create_trigger_error_hist = False
        self.create_error_hist = True
        self.create_service_record_hist = True
        self.create_threshold_hists = False
        self.create_threshold_mask = True  # threshold/noise histogram mask: masking all pixels out of bounds
        self.create_fitted_threshold_mask = True  # fitted threshold/noise histogram mask: masking all pixels out of bounds
        self.create_fitted_threshold_hists = False
        self.create_cluster_hit_table = False
        self.create_cluster_table = False
        self.create_cluster_size_hist = False
        self.create_cluster_tot_hist = False
        self._n_injection = 100
        self.n_bcid = 16
        self._max_tot_value = 13

    @property
    def chunk_size(self):
        return self._chunk_size

    @chunk_size.setter
    def chunk_size(self, value):
        self._chunk_size = value

    @property
    def create_hit_table(self):
        return self._create_hit_table

    @create_hit_table.setter
    def create_hit_table(self, value):
        self._create_hit_table = value

    @property
    def create_occupancy_hist(self):
        return self._create_occupancy_hist

    @create_occupancy_hist.setter
    def create_occupancy_hist(self, value):
        self._create_occupancy_hist = value
        self.histograming.create_occupancy_hist(value)

    @property
    def create_source_scan_hist(self):
        return self._create_source_scan_hist

    @create_source_scan_hist.setter
    def create_source_scan_hist(self, value):
        self._create_source_scan_hist = value

    @property
    def create_tot_hist(self):
        return self._create_occupancy_hist

    @create_tot_hist.setter
    def create_tot_hist(self, value):
        self._create_tot_hist = value
        self.histograming.create_tot_hist(value)

    @property
    def create_rel_bcid_hist(self):
        return self._create_rel_bcid_hist

    @create_rel_bcid_hist.setter
    def create_rel_bcid_hist(self, value):
        self._create_rel_bcid_hist = value
        self.histograming.create_rel_bcid_hist(value)

    @property
    def create_threshold_hists(self):
        return self._create_threshold_hists

    @create_threshold_hists.setter
    def create_threshold_hists(self, value):
        self._create_threshold_hists = value

    @property
    def create_threshold_mask(self):
        return self._create_threshold_mask

    @create_threshold_mask.setter
    def create_threshold_mask(self, value):
        self._create_threshold_mask = value

    @property
    def create_fitted_threshold_mask(self):
        return self._create_fitted_threshold_mask

    @create_fitted_threshold_mask.setter
    def create_fitted_threshold_mask(self, value):
        self._create_fitted_threshold_mask = value

    @property
    def create_fitted_threshold_hists(self):
        return self._create_fitted_threshold_hists

    @create_fitted_threshold_hists.setter
    def create_fitted_threshold_hists(self, value):
        self._create_fitted_threshold_hists = value

    @property
    def create_error_hist(self):
        return self._create_error_hist

    @create_error_hist.setter
    def create_error_hist(self, value):
        self._create_error_hist = value

    @property
    def create_trigger_error_hist(self):
        return self._create_trigger_error_hist

    @create_trigger_error_hist.setter
    def create_trigger_error_hist(self, value):
        self._create_trigger_error_hist = value

    @property
    def create_service_record_hist(self):
        return self._create_service_record_hist

    @create_service_record_hist.setter
    def create_service_record_hist(self, value):
        self._create_service_record_hist = value

    @property
    def create_meta_event_index(self):
        return self._create_meta_event_index

    @create_meta_event_index.setter
    def create_meta_event_index(self, value):
        self._create_meta_event_index = value

    @property
    def create_meta_word_index(self):
        return self._create_meta_word_index

    @create_meta_word_index.setter
    def create_meta_word_index(self, value):
        self._create_meta_word_index = value
        self.interpreter.create_meta_data_word_index(value)

    @property
    def FEI4B(self):
        return self._FEI4B

    @FEI4B.setter
    def FEI4B(self, value):
        self._FEI4B = value
        self.interpreter.set_FEI4B(value)

    @property
    def n_injections(self):
        """Get the numbers of injections per pixel."""
        return self._n_injection

    @n_injections.setter
    def n_injections(self, value):
        """Set the numbers of injections per pixel."""
        self._n_injection = value

    @property
    def n_bcid(self):
        """Get the numbers of BCIDs (usually 16) of one event."""
        return self._n_bcid

    @n_bcid.setter
    def n_bcid(self, value):
        """Set the numbers of BCIDs (usually 16) of one event."""
        _n_bcid = value
        self.interpreter.set_trig_count(_n_bcid)

    @property
    def max_tot_value(self):
        """Get maximum ToT value that is considered to be a hit"""
        return self._max_tot_value

    @max_tot_value.setter
    def max_tot_value(self, value):
        """Set maximum ToT value that is considered to be a hit"""
        self._max_tot_value = value
        self.histograming.set_max_tot(self._max_tot_value)

    @property
    def create_cluster_hit_table(self):
        return self._create_cluster_hit_table

    @create_cluster_hit_table.setter
    def create_cluster_hit_table(self, value):
        self._create_cluster_hit_table = value
        self.clusterizer.create_cluster_hit_info_array(value)

    @property
    def create_cluster_table(self):
        return self._create_cluster_table

    @create_cluster_table.setter
    def create_cluster_table(self, value):
        self._create_cluster_table = value
        self.clusterizer.create_cluster_info_array(value)

    @property
    def create_cluster_size_hist(self):
        return self._create_cluster_size_hist

    @create_cluster_size_hist.setter
    def create_cluster_size_hist(self, value):
        self._create_cluster_size_hist = value

    @property
    def create_cluster_tot_hist(self):
        return self._create_cluster_tot_hist

    @create_cluster_tot_hist.setter
    def create_cluster_tot_hist(self, value):
        self._create_cluster_tot_hist = value

    #@profile
    def interpret_word_table(self, raw_data_file=None, analyzed_data_file=None, FEI4B=False):
        if(raw_data_file != None):
            self._raw_data_file = raw_data_file

        if(analyzed_data_file != None):
            self._analyzed_data_file = analyzed_data_file

        self.FEI4B = FEI4B

        hits = np.empty((self._chunk_size,), dtype=dtype_from_descr(data_struct.HitInfoTable))

        if(self._create_meta_word_index):
            meta_word = np.empty((self._chunk_size,), dtype=dtype_from_descr(data_struct.MetaInfoWordTable))
            self.interpreter.set_meta_data_word_index(meta_word)

        if(self.create_cluster_hit_table or self.create_cluster_table):
            cluster_hits = np.empty((2 * self._chunk_size,), dtype=dtype_from_descr(data_struct.ClusterHitInfoTable))
            cluster = np.empty((2 * self._chunk_size,), dtype=dtype_from_descr(data_struct.ClusterInfoTable))
            self.clusterizer.set_cluster_hit_info_array(cluster_hits)
            self.clusterizer.set_cluster_info_array(cluster)

        logging.info('Interpreting:')
        self._filter_table = tb.Filters(complib='blosc', complevel=5, fletcher32=False)
        with tb.openFile(self._raw_data_file, mode="r") as in_file_h5:
            if(self._analyzed_data_file != None):
                self.out_file_h5 = tb.openFile(self._analyzed_data_file, mode="w", title="Interpreted FE-I4 raw data")
                if (self._create_hit_table == True):
                    hit_table = self.out_file_h5.createTable(self.out_file_h5.root, name='Hits', description=data_struct.HitInfoTable, title='hit_data', filters=self._filter_table, chunkshape=(self._chunk_size / 100,))
                if (self._create_meta_word_index == True):
                    meta_word_index_table = self.out_file_h5.createTable(self.out_file_h5.root, name='EventMetaData', description=data_struct.MetaInfoWordTable, title='event_meta_data', filters=self._filter_table, chunkshape=(self._chunk_size / 10,))
                if(self._create_cluster_table):
                    cluster_table = self.out_file_h5.createTable(self.out_file_h5.root, name='Cluster', description=data_struct.ClusterInfoTable, title='cluster_hit_data', filters=self._filter_table, expectedrows=self._chunk_size)
                if(self._create_cluster_hit_table):
                    cluster_hit_table = self.out_file_h5.createTable(self.out_file_h5.root, name='ClusterHits', description=data_struct.ClusterHitInfoTable, title='cluster_hit_data', filters=self._filter_table, expectedrows=self._chunk_size)
            self.meta_data = in_file_h5.root.meta_data[:]
            try:
                self.scan_parameters = in_file_h5.root.scan_parameters[:]
                self.histograming.add_scan_parameter(self.scan_parameters)
            except tb.exceptions.NoSuchNodeError:
                self.scan_parameters = None
                self.histograming.set_no_scan_parameter()

            table_size = in_file_h5.root.raw_data.shape[0]
            meta_data_size = self.meta_data.shape[0]

            self.interpreter.reset_event_variables()
            self.interpreter.reset_counters()
            self.interpreter.set_hits_array(hits)
            self.interpreter.set_meta_data(self.meta_data)

            self.meta_event_index = np.zeros((meta_data_size,), dtype=[('metaEventIndex', np.uint32)])
            self.interpreter.set_meta_event_data(self.meta_event_index)

            for iWord in range(0, table_size, self._chunk_size):
                raw_data = in_file_h5.root.raw_data.read(iWord, iWord + self._chunk_size)
                self.interpreter.interpret_raw_data(raw_data)
                if(iWord == range(0, table_size, self._chunk_size)[-1]):  # store hits of the latest event
                    self.interpreter.store_event()
                Nhits = self.interpreter.get_n_array_hits()
                if(self.scan_parameters != None):
                    nEventIndex = self.interpreter.get_n_meta_data_event()
                    self.histograming.add_meta_event_index(self.meta_event_index, nEventIndex)
                if self.is_histogram_hits():
                    self.histogram_hits(hits[:Nhits], stop_index=Nhits)
                if self.is_cluster_hits():
                    self.cluster_hits(hits[:Nhits])
                    if(self._create_cluster_hit_table):
                        cluster_hit_table.append(cluster_hits[:Nhits])
                    if(self._create_cluster_table):
                        cluster_table.append(cluster[:self.clusterizer.get_n_clusters()])

                if (self._analyzed_data_file != None and self._create_hit_table == True):
                    hit_table.append(hits[:Nhits])
                if (self._analyzed_data_file != None and self._create_meta_word_index == True):
                    size = self.interpreter.get_n_meta_data_word()
                    meta_word_index_table.append(meta_word[:size])

                logging.info('%d %%' % int(float(float(iWord) / float(table_size) * 100.)))

            if (self._analyzed_data_file != None and self._create_hit_table == True):
                hit_table.flush()
            logging.info('100 %')
            self._create_additional_data()
            if(self._analyzed_data_file != None):
                self.out_file_h5.close()
        del hits

    def _create_additional_data(self):
        logging.info('Create selected event histograms')
        if (self._analyzed_data_file != None and self._create_meta_event_index):
            meta_data_size = self.meta_data.shape[0]
            nEventIndex = self.interpreter.get_n_meta_data_event()
            if (meta_data_size == nEventIndex):
                if self.interpreter.meta_table_v2:
                    description = data_struct.MetaInfoEventTableV2().columns
                else:
                    description = data_struct.MetaInfoEventTable().columns
                last_pos = len(description)
                if (self.scan_parameters != None):  # add additional column with the scan parameter
                    for scan_par_name in self.scan_parameters.dtype.names:
                        dtype, pos = self.scan_parameters.dtype.fields[scan_par_name][:2]
                        if pos == 0:
                            description[scan_par_name] = Col.from_dtype(dtype, dflt=0, pos=last_pos)  # TODO: support more scan parameters
                            break
                meta_data_out_table = self.out_file_h5.createTable(self.out_file_h5.root, name='meta_data', description=description, title='MetaData', filters=self._filter_table)
                entry = meta_data_out_table.row
                for i in range(0, nEventIndex):
                    if self.interpreter.meta_table_v2:
                        entry['event_number'] = self.meta_event_index[i][0]  # event index
                        entry['timestamp_start'] = self.meta_data[i][3]  # timestamp
                        entry['timestamp_stop'] = self.meta_data[i][4]  # timestamp
                        entry['error_code'] = self.meta_data[i][5]  # error code
                    else:
                        entry['event_number'] = self.meta_event_index[i][0]  # event index
                        entry['time_stamp'] = self.meta_data[i][3]  # time stamp
                        entry['error_code'] = self.meta_data[i][4]  # error code
                    if (self.scan_parameters != None):  # scan parameter if available
                        entry[scan_par_name] = self.scan_parameters[i][0]
                    entry.append()
                meta_data_out_table.flush()
                if self.scan_parameters != None:
                    logging.info("Save meta data with scan parameter " + scan_par_name)
            else:
                logging.error('Meta data analysis failed')
        if (self._create_service_record_hist):
            self.service_record_hist = np.zeros(32, dtype=np.uint32)  # IMPORTANT: has to be global to avoid deleting before c library is deleted
            self.interpreter.get_service_records_counters(self.service_record_hist)
            if (self._analyzed_data_file != None):
                service_record_hist_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistServiceRecord', title='Service Record Histogram', atom=tb.Atom.from_dtype(self.service_record_hist.dtype), shape=self.service_record_hist.shape, filters=self._filter_table)
                service_record_hist_table[:] = self.service_record_hist
        if (self._create_error_hist):
            self.error_counter_hist = np.zeros(16, dtype=np.uint32)
            self.interpreter.get_error_counters(self.error_counter_hist)
            if (self._analyzed_data_file != None):
                error_counter_hist_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistErrorCounter', title='Error Counter Histogram', atom=tb.Atom.from_dtype(self.error_counter_hist.dtype), shape=self.error_counter_hist.shape, filters=self._filter_table)
                error_counter_hist_table[:] = self.error_counter_hist
        if (self._create_trigger_error_hist):
            self.trigger_error_counter_hist = np.zeros(8, dtype=np.uint32)
            self.interpreter.get_trigger_error_counters(self.trigger_error_counter_hist)
            if (self._analyzed_data_file != None):
                trigger_error_counter_hist_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistTriggerErrorCounter', title='Trigger Error Counter Histogram', atom=tb.Atom.from_dtype(self.trigger_error_counter_hist.dtype), shape=self.trigger_error_counter_hist.shape, filters=self._filter_table)
                trigger_error_counter_hist_table[:] = self.trigger_error_counter_hist

        self._create_additional_hit_data()
        self._create_additional_cluster_data()

    def _create_additional_hit_data(self):
        logging.info('Create selected hit histograms')
        if (self._create_tot_hist):
            self.tot_hist = np.zeros(16, dtype=np.uint32)
            self.histograming.get_tot_hist(self.tot_hist)
            if (self._analyzed_data_file != None):
                tot_hist_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistTot', title='ToT Histogram', atom=tb.Atom.from_dtype(self.tot_hist.dtype), shape=self.tot_hist.shape, filters=self._filter_table)
                tot_hist_table[:] = self.tot_hist
        if (self._create_rel_bcid_hist):
            self.rel_bcid_hist = np.zeros(16, dtype=np.uint32)
            self.histograming.get_rel_bcid_hist(self.rel_bcid_hist)
            if (self._analyzed_data_file != None):
                rel_bcid_hist_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistRelBcid', title='relative BCID Histogram', atom=tb.Atom.from_dtype(self.rel_bcid_hist.dtype), shape=self.rel_bcid_hist.shape, filters=self._filter_table)
                rel_bcid_hist_table[:] = self.rel_bcid_hist
        if (self._create_occupancy_hist):
            self.occupancy = np.zeros(80 * 336 * self.histograming.get_n_parameters(), dtype=np.uint32)  # create linear array as it is created in histogram class
            self.histograming.get_occupancy(self.occupancy)
            occupancy_array = np.reshape(a=self.occupancy.view(), newshape=(80, 336, self.histograming.get_n_parameters()), order='F')  # make linear array to 3d array (col,row,parameter)
            self.occupancy_array = np.swapaxes(occupancy_array, 0, 1)
            if (self._analyzed_data_file != None):
                occupancy_array_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistOcc', title='Occupancy Histogram', atom=tb.Atom.from_dtype(self.occupancy.dtype), shape=(336, 80, self.histograming.get_n_parameters()), filters=self._filter_table)
                occupancy_array_table[0:336, 0:80, 0:self.histograming.get_n_parameters()] = self.occupancy_array  # swap axis col,row,parameter --> row, col,parameter
        if (self._create_threshold_hists):
            threshold = np.zeros(80 * 336, dtype=np.float64)
            noise = np.zeros(80 * 336, dtype=np.float64)
            # calling fast algorithm function: M. Mertens, PhD thesis, Juelich 2010
            # note: noise zero if occupancy was zero
            self.histograming.calculate_threshold_scan_arrays(threshold, noise, self._n_injection)
            threshold_hist = np.reshape(a=threshold.view(), newshape=(80, 336), order='F')
            noise_hist = np.reshape(a=noise.view(), newshape=(80, 336), order='F')
            self.threshold_hist = np.swapaxes(threshold_hist, 0, 1)
            self.noise_hist = np.swapaxes(noise_hist, 0, 1)
            if (self._analyzed_data_file != None):
#                 if self._create_threshold_mask:
#                     threshold_mask_table = self.out_file_h5.createCArray(self.out_file_h5.root, name = 'MaskThreshold', title = 'Threshold Mask', atom = tb.Atom.from_dtype(self.threshold_mask.dtype), shape = (336,80), filters = self._filter_table)
#                     threshold_mask_table[0:336, 0:80] = self.threshold_mask
                threshold_hist_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistThreshold', title='Threshold Histogram', atom=tb.Atom.from_dtype(self.threshold_hist.dtype), shape=(336, 80), filters=self._filter_table)
                threshold_hist_table[0:336, 0:80] = self.threshold_hist
                noise_hist_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistNoise', title='Noise Histogram', atom=tb.Atom.from_dtype(self.noise_hist.dtype), shape=(336, 80), filters=self._filter_table)
                noise_hist_table[0:336, 0:80] = self.noise_hist
#             if self._create_threshold_mask:
#                 self.threshold_mask = generate_threshold_mask(self.noise_hist)
        if (self._create_fitted_threshold_hists):
            scan_parameters = np.linspace(self.histograming.get_min_parameter(), self.histograming.get_max_parameter(), num=self.histograming.get_n_parameters(), endpoint=True)
            self.scurve_fit_results = self.fit_scurves_multithread(self.out_file_h5, PlsrDAC=scan_parameters)
            if (self._analyzed_data_file != None):
                fitted_threshold_hist_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistThresholdFitted', title='Threshold Fitted Histogram', atom=tb.Atom.from_dtype(self.scurve_fit_results.dtype), shape=(336, 80), filters=self._filter_table)
                fitted_threshold_hist_table[0:336, 0:80] = self.scurve_fit_results[:, :, 0]
                fitted_noise_hist_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistNoiseFitted', title='Noise Fitted Histogram', atom=tb.Atom.from_dtype(self.scurve_fit_results.dtype), shape=(336, 80), filters=self._filter_table)
                fitted_noise_hist_table[0:336, 0:80] = self.scurve_fit_results[:, :, 1]
#             if self._create_fitted_threshold_mask:
#                 self.fitted_threshold_mask = generate_threshold_mask(self.scurve_fit_results[:, :, 1])

    def _create_additional_cluster_data(self):
        logging.info('Create selected cluster histograms')
        if(self._create_cluster_size_hist):
            self.cluster_size_hist = np.zeros(1024, dtype=np.uint32)
            self.clusterizer.get_cluster_size_hist(self.cluster_size_hist)
            if (self._analyzed_data_file != None):
                cluster_size_hist_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistClusterSize', title='Cluster Size Histogram', atom=tb.Atom.from_dtype(self.cluster_size_hist.dtype), shape=self.cluster_size_hist.shape, filters=self._filter_table)
                cluster_size_hist_table[:] = self.cluster_size_hist
        if(self._create_cluster_tot_hist):
            cluster_tot_hist = np.zeros(128 * 1024, dtype=np.uint32)  # create linear array as it is created in histogram class
            self.clusterizer.get_cluster_tot_hist(cluster_tot_hist)
            self.cluster_tot_hist = np.reshape(a=cluster_tot_hist.view(), newshape=(128, 1024), order='F')  # make linear array to 2d array (tot, cluster size)
            if (self._analyzed_data_file != None):
                cluster_tot_hist_table = self.out_file_h5.createCArray(self.out_file_h5.root, name='HistClusterTot', title='Cluster Tot Histogram', atom=tb.Atom.from_dtype(self.cluster_tot_hist.dtype), shape=self.cluster_tot_hist.shape, filters=self._filter_table)
                cluster_tot_hist_table[:] = self.cluster_tot_hist

    # @profile
    def analyze_hit_table(self, analyzed_data_file=None, analyzed_data_out_file=None):
        cluster_hits = np.empty((2 * self._chunk_size,), dtype=dtype_from_descr(data_struct.ClusterHitInfoTable))
        cluster = np.empty((2 * self._chunk_size,), dtype=dtype_from_descr(data_struct.ClusterInfoTable))

        in_file_h5 = None

        # set output file if an output file name is given, otherwise check if an output file is already opened
        if analyzed_data_out_file != None:  # if an output file name is specified create new file for analyzed data
            if self.is_open(self.out_file_h5):
                self.out_file_h5.close()
            self.out_file_h5 = tb.openFile(analyzed_data_out_file, mode="w", title="Analyzed FE-I4 hits")
        elif self._analyzed_data_file != None:  # if no output file is specified check if an output file is already open and write new data into the opened one
            if not self.is_open(self.out_file_h5):
                self.out_file_h5 = tb.openFile(self._analyzed_data_file, mode="r+")
                in_file_h5 = self.out_file_h5  # input file is output file
        else:
            print self.out_file_h5

        if analyzed_data_file != None:
            self._analyzed_data_file = analyzed_data_file
        elif (self._analyzed_data_file == None):
            logging.warning("No data file with analyzed data given, abort!")
            return

        if in_file_h5 == None:
            in_file_h5 = tb.openFile(self._analyzed_data_file, mode="r")

        if(self._create_cluster_table):
            cluster_table = self.out_file_h5.createTable(self.out_file_h5.root, name='Cluster', description=data_struct.ClusterInfoTable, title='cluster_hit_data', filters=self._filter_table, expectedrows=self._chunk_size)
            self.clusterizer.set_cluster_info_array(cluster)
        if(self._create_cluster_hit_table):
            cluster_hit_table = self.out_file_h5.createTable(self.out_file_h5.root, name='ClusterHits', description=data_struct.ClusterHitInfoTable, title='cluster_hit_data', filters=self._filter_table, expectedrows=self._chunk_size)
            self.clusterizer.set_cluster_hit_info_array(cluster_hits)

        try:
            meta_data_table = in_file_h5.root.meta_data
            meta_data = meta_data_table[:]
            if (meta_data.dtype.names.index('error_code') < len(meta_data.dtype.names) - 1):
                meta_data_array = np.array(meta_data['event_number'], dtype=[('metaEventIndex', np.uint32)])
                self.histograming.add_meta_event_index(meta_data_array, array_length=len(meta_data_table))
                scan_par_name = meta_data.dtype.names[meta_data.dtype.names.index('error_code') + 1]
                scan_par_array = np.array(meta_data[scan_par_name], dtype=[(scan_par_name, '<u4'), ])
                self.histograming.add_scan_parameter(scan_par_array)
                logging.info("Add scan parameter " + scan_par_name + " for analysis")
            else:
                logging.info("No scan parameter data provided")
                self.histograming.set_no_scan_parameter()
        except tb.exceptions.NoSuchNodeError:
            logging.info("No meta data provided")

        table_size = in_file_h5.root.Hits.shape[0]
        last_event_start_index = 0
        n_hits = 0  # number of hits in actual chunk

        logging.info('Analyze hits:')
        for iHit in range(0, table_size, self._chunk_size):
            offset = last_event_start_index - n_hits if iHit != 0 else 0  # reread last hits of last event of last chunk
            hits = in_file_h5.root.Hits.read(iHit + offset, iHit + self._chunk_size)
            n_hits = hits.shape[0]

            # align hits at events, omit last event
            last_event = hits["event_number"][-1]
            last_event_start_index = np.where(hits["event_number"] == last_event)[0][0]

            # do not omit the last words of the last events of the last chunk
            if(iHit == range(0, table_size, self._chunk_size)[-1]):
                last_event_start_index = n_hits

            if (self.is_cluster_hits()):
                self.cluster_hits(hits, stop_index=last_event_start_index)

            if (self.is_histogram_hits()):
                self.histogram_hits(hits, stop_index=last_event_start_index)

            if(self._analyzed_data_file != None and self._create_cluster_hit_table):
                cluster_hit_table.append(cluster_hits[:last_event_start_index])
            if(self._analyzed_data_file != None and self._create_cluster_table):
                cluster_table.append(cluster[:self.clusterizer.get_n_clusters()])

            logging.info('%d %%' % int(float(float(iHit) / float(table_size) * 100.)))

        logging.info('100 %')
        self._create_additional_hit_data()
        self._create_additional_cluster_data()

        self.out_file_h5.close()
        in_file_h5.close()

    def cluster_hits(self, hits, start_index=0, stop_index=-1): #FIXME: std setting omits always the last hits
        self.clusterizer.add_hits(hits[start_index:stop_index])

    def histogram_hits(self, hits, start_index=0, stop_index=-1): #FIXME: std setting omits always the last hits
        self.histograming.add_hits(hits[start_index:stop_index], hits[start_index:stop_index].shape[0])

    def plot_histograms(self, scan_data_filename, analyzed_data_file=None, maximum=None):  # plots the histogram from output file if available otherwise from ram
        logging.info('Creating histograms%s' % ((' (source: %s)' % analyzed_data_file) if analyzed_data_file != None else ((' (source: %s)' % self._analyzed_data_file) if self._analyzed_data_file != None else '')))
        if analyzed_data_file != None:
            out_file_h5 = tb.openFile(analyzed_data_file, mode="r")
        elif(self._analyzed_data_file != None):
            out_file_h5 = tb.openFile(self._analyzed_data_file, mode="r")
        else:
            out_file_h5 = None
#         if os.path.splitext(scan_data_filename)[1].strip().lower() != ".pdf":  # check for correct filename extension
#             output_pdf_filename = os.path.splitext(scan_data_filename)[0] + ".pdf"
        if scan_data_filename[len(scan_data_filename) - 3:] != ".pdf":  # check for correct filename extension
            output_pdf_filename = scan_data_filename + ".pdf"
        else:
            output_pdf_filename = scan_data_filename
        logging.info('Saving output file: %s' % output_pdf_filename)
        output_pdf = PdfPages(output_pdf_filename)
        if (self._create_threshold_hists):
            # use threshold mask if possible
            if self._create_threshold_mask:
                if out_file_h5 != None:
                    self.threshold_mask = generate_threshold_mask(out_file_h5.root.HistNoise[:, :])
                else:
                    self.threshold_mask = generate_threshold_mask(self.noise_hist)
                threshold_hist = np.ma.array(out_file_h5.root.HistThreshold[:, :] if out_file_h5 != None else self.threshold_hist, mask=self.threshold_mask)
                noise_hist = np.ma.array(out_file_h5.root.HistNoise[:, :] if out_file_h5 != None else self.noise_hist, mask=self.threshold_mask)
                mask_cnt = np.ma.count_masked(noise_hist)
                logging.info('Fast algorithm: masking %d pixel(s)' % mask_cnt)
            else:
                threshold_hist = out_file_h5.root.HistThreshold[:, :] if out_file_h5 != None else self.threshold_hist
                noise_hist = out_file_h5.root.HistNoise[:, :] if out_file_h5 != None else self.noise_hist
            plotting.plotThreeWay(hist=threshold_hist, title='Threshold%s' % ((' (masked %i pixel(s))' % mask_cnt) if self._create_threshold_mask else ''), x_axis_title="threshold [PlsrDAC]", filename=output_pdf, bins=100, minimum=0, maximum=maximum)
            plotting.plotThreeWay(hist=noise_hist, title='Noise%s' % ((' (masked %i pixel(s))' % mask_cnt) if self._create_threshold_mask else ''), x_axis_title="noise [PlsrDAC]", filename=output_pdf, bins=100, minimum=0, maximum=maximum)
        if (self._create_fitted_threshold_hists):
            if self._create_fitted_threshold_mask:
                if out_file_h5 != None:
                    self.fitted_threshold_mask = generate_threshold_mask(out_file_h5.root.HistNoiseFitted[:, :])
                else:
                    self.fitted_threshold_mask = generate_threshold_mask(self.scurve_fit_results[:, :, 1])
                threshold_hist = np.ma.array(out_file_h5.root.HistThresholdFitted[:, :] if out_file_h5 != None else self.scurve_fit_results[:, :, 0], mask=self.fitted_threshold_mask)
                noise_hist = np.ma.array(out_file_h5.root.HistNoiseFitted[:, :] if out_file_h5 != None else self.scurve_fit_results[:, :, 1], mask=self.fitted_threshold_mask)
                mask_cnt = np.ma.count_masked(noise_hist)
                logging.info('S-curve fit: masking %d pixel(s)' % mask_cnt)
            else:
                threshold_hist = out_file_h5.root.HistThresholdFitted[:, :] if out_file_h5 != None else self.scurve_fit_results[:, :, 0]
                noise_hist = out_file_h5.root.HistNoiseFitted[:, :] if out_file_h5 != None else self.scurve_fit_results[:, :, 1]
            plotting.plotThreeWay(hist=threshold_hist, title='Threshold (S-curve fit%s' % ((', masked %i pixel(s))' % mask_cnt) if self._create_fitted_threshold_mask else ')'), x_axis_title="threshold [PlsrDAC]", filename=output_pdf, bins=100, minimum=0, maximum=maximum)
            plotting.plotThreeWay(hist=noise_hist, title='Noise (S-curve fit%s' % ((', masked %i pixel(s))' % mask_cnt) if self._create_fitted_threshold_mask else ')'), x_axis_title="noise [PlsrDAC]", filename=output_pdf, bins=100, minimum=0, maximum=maximum)
        if (self._create_occupancy_hist):
            if(self._create_threshold_hists):
                plotting.plot_scurves(occupancy_hist=out_file_h5.root.HistOcc[:, :, :] if out_file_h5 != None else self.occupancy_array[:, :, :], filename=output_pdf, scan_parameters=np.linspace(self.histograming.get_min_parameter(), self.histograming.get_max_parameter(), num=self.histograming.get_n_parameters(), endpoint=True))
            else:
                hist = out_file_h5.root.HistOcc[:, :, 0] if out_file_h5 != None else self.occupancy_array[:, :, 0]
                occupancy_array_masked = np.ma.masked_equal(hist, 0)
                if self._create_source_scan_hist:
                    plotting.plot_fancy_occupancy(hist=occupancy_array_masked, filename=output_pdf, z_max='maximum')
                    plotting.plot_occupancy(hist=occupancy_array_masked, filename=output_pdf, z_max='maximum')
                else:
                    plotting.plotThreeWay(hist=occupancy_array_masked, title="Occupancy", x_axis_title="occupancy", filename=output_pdf, maximum=maximum)
                    plotting.plot_occupancy(hist=occupancy_array_masked, filename=output_pdf, z_max='median')
        if (self._create_tot_hist):
            plotting.plot_tot(hist=out_file_h5.root.HistTot if out_file_h5 != None else self.tot_hist, filename=output_pdf)
        if (self._create_cluster_size_hist):
            plotting.plot_cluster_size(hist=out_file_h5.root.HistClusterSize if out_file_h5 != None else self.cluster_size_hist, filename=output_pdf)
        if (self._create_cluster_tot_hist):
            plotting.plot_cluster_tot(hist=out_file_h5.root.HistClusterTot if out_file_h5 != None else self.cluster_tot_hist, filename=output_pdf)
        if (self._create_cluster_tot_hist and self._create_cluster_size_hist):
            plotting.plot_cluster_tot_size(hist=out_file_h5.root.HistClusterTot if out_file_h5 != None else self.cluster_tot_hist, filename=output_pdf)
        if (self._create_rel_bcid_hist):
            plotting.plot_relative_bcid(hist=out_file_h5.root.HistRelBcid if out_file_h5 != None else self.rel_bcid_hist, filename=output_pdf)
        if (analyzed_data_file == None and self._create_error_hist):
            plotting.plot_event_errors(hist=out_file_h5.root.HistErrorCounter if out_file_h5 != None else self.error_counter_hist, filename=output_pdf)
        if (analyzed_data_file == None and self._create_service_record_hist):
            plotting.plot_service_records(hist=out_file_h5.root.HistServiceRecord if out_file_h5 != None else self.service_record_hist, filename=output_pdf)
        if (analyzed_data_file == None and self._create_trigger_error_hist):
            plotting.plot_trigger_errors(hist=out_file_h5.root.HistTriggerErrorCounter if out_file_h5 != None else self.trigger_error_counter_hist, filename=output_pdf)
        if (self._analyzed_data_file != None):
            out_file_h5.close()
        logging.info('Closing output file')
        output_pdf.close()

    def fit_scurves(self, hit_table_file=None, PlsrDAC=None):
        occupancy_hist = hit_table_file.root.HistOcc[:, :, :] if hit_table_file != None else self.occupancy_array[:, :, :]  # take data from RAM if no file was opened
        occupancy_hist_shaped = occupancy_hist.reshape(occupancy_hist.shape[0] * occupancy_hist.shape[1], occupancy_hist.shape[2])
        result_array = np.array(fit_scurves_subset(occupancy_hist_shaped[:], PlsrDAC=PlsrDAC))
        return result_array.reshape(occupancy_hist.shape[0], occupancy_hist.shape[1], 2)

    def fit_scurves_multithread(self, hit_table_file=None, PlsrDAC=None):
        logging.info("Start S-curve fit on %d CPU cores" % mp.cpu_count())
        occupancy_hist = hit_table_file.root.HistOcc[:, :, :] if hit_table_file != None else self.occupancy_array[:, :, :]  # take data from RAM if no file is opened
        occupancy_hist_shaped = occupancy_hist.reshape(occupancy_hist.shape[0] * occupancy_hist.shape[1], occupancy_hist.shape[2])
        partialfit_scurve = partial(fit_scurve, PlsrDAC=PlsrDAC)  # trick to give a function more than one parameter, needed for pool.map
        pool = mp.Pool(processes=mp.cpu_count())  # create as many workers as physical cores are available
        try:
            result_list = pool.map(partialfit_scurve, occupancy_hist_shaped.tolist())
        except TypeError:
            raise Exception('S-curve fit needs at least three data points')
        pool.close()
        pool.join()  # blocking function until fit finished
        result_array = np.array(result_list)
        logging.info("S-curve fit finished")
        return result_array.reshape(occupancy_hist.shape[0], occupancy_hist.shape[1], 2)

    def is_open(self, h5_file):
        try:  # check if output h5 file is already opened
            h5_file.root
        except AttributeError:
            return False
        return True

    def is_histogram_hits(self):  # returns true if a setting needs to have the hit histogramming active
        if (self._analyzed_data_file != None and (self._create_occupancy_hist or self._create_tot_hist or self._create_rel_bcid_hist or self._create_hit_table or self._create_threshold_hists or self._create_fitted_threshold_hists)):
            return True
        return False

    def is_cluster_hits(self):  # returns true if a setting needs to have the clusterizer active
        if (self._analyzed_data_file != None and (self.create_cluster_hit_table or self.create_cluster_table or self.create_cluster_size_hist or self.create_cluster_tot_hist)):
            return True
        return False

if __name__ == "__main__":
    mp.freeze_support()
    scan_name = 'scan_threshold_14_interpreted'
    chip_flavor = 'fei4a'
    input_file = r"fake_data.h5"
    output_file = r"C:\pybar\trunk\host\data/test.h5"
    scan_data_filename = r"C:\pybar\trunk\host\data/" + scan_name + ".h5"

    with AnalyzeRawData(raw_data_file=scan_data_filename, analyzed_data_file=output_file) as analyze_raw_data:
        with tb.openFile(analyze_raw_data._raw_data_file, mode="r") as in_file_h5:
            analyze_raw_data.fit_scurves_multithread(in_file_h5, PlsrDAC=range(0, 51))
