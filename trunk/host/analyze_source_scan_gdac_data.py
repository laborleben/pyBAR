import numpy as np
import tables as tb
from scipy.interpolate import interp1d

from analysis.plotting.plotting import plot_profile_histogram, plot_scatter
from analysis import analysis_utils

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - [%(levelname)-8s] (%(threadName)-10s) %(message)s")


def get_mean_threshold(gdac, mean_threshold_calibration):
    interpolation = interp1d(mean_threshold_calibration['gdac'], mean_threshold_calibration['mean_threshold'], kind='slinear', bounds_error=True)
    return interpolation(gdac)


def get_pixel_thresholds_from_table(column, row, gdacs, threshold_calibration_table):
    pixel_gdacs = threshold_calibration_table[np.logical_and(threshold_calibration_table['column'] == column, threshold_calibration_table['row'] == row)]['gdac']
    pixel_thresholds = threshold_calibration_table[np.logical_and(threshold_calibration_table['column'] == column, threshold_calibration_table['row'] == row)]['threshold']
    interpolation = interp1d(x=pixel_gdacs, y=pixel_thresholds, kind='slinear', bounds_error=True)
    return interpolation(gdacs)


def get_pixel_thresholds(gdacs, calibration_gdacs, threshold_calibration_array):
    '''Calculates the threshold for all pixels in threshold_calibration_array at the given GDAC settings via linear interpolation. The GDAC settings used during calibration have to be given.

    Parameters
    ----------
    gdacs : array like
        The GDAC settings where the threshold should be determined from the calibration
    calibration_gdacs : array like
        GDAC settings used during calibration, needed to translate the index of the calibration array to a value.
    threshold_calibration_array : numpy.array, shape=(80,336,# of GDACs during calibration)
        The calibration array

    Returns
    -------
    numpy.array, shape=(80,336,# gdacs given)
        The threshold values for each pixel at gdacs.
    '''
    if len(calibration_gdacs) != threshold_calibration_array.shape[2]:
        raise ValueError('Length of the provided pixel GDACs does not match the third dimension of the calibration array')
    interpolation = interp1d(x=calibration_gdacs, y=threshold_calibration_array, kind='slinear', bounds_error=True)
    return interpolation(gdacs)


def get_hit_rate_correction(gdacs, calibration_gdacs, cluster_size_histogram):
    '''Calculates a correction factor for single hit clusters at the given GDACs from the cluster_size_histogram via cubic interpolation.

    Parameters
    ----------
    gdacs : array like
        The GDAC settings where the threshold should be determined from the calibration
    calibration_gdacs : array like
        GDAC settings used during the source scan for the cluster size calibration.
    cluster_size_histogram : numpy.array, shape=(80,336,# of GDACs during calibration)
        The calibration array

    Returns
    -------
    numpy.array, shape=(80,336,# of GDACs during calibration)
        The threshold values for each pixel at gdacs.
    '''

    logging.info('Calculate the correction factor for the single hit cluster rate at %d given GDAC settings' % len(gdacs))
    hist_sum = np.sum(cluster_size_histogram, axis=1)
    hist_rel = cluster_size_histogram / hist_sum[:, np.newaxis] * 100
    maximum_rate = np.amax(hist_rel[:-2, 1])
    correction_factor = maximum_rate / hist_rel[:-2, 1]
    interpolation = interp1d(calibration_gdacs[:-1], correction_factor, kind='cubic', bounds_error=True)
    return interpolation(gdacs)


if __name__ == "__main__":
    
    
    input_file_hits = 'data/' + scan_name + "_cut_3_analyzed.h5"
    input_file_calibration = 'data/calibrate_threshold_gdac.h5'
    input_file_correction = 'data/scan_fei4_trigger_141_analyzed_per_parameter_2.h5'
    
    scan_name = 'bias_20\\scan_fei4_trigger_gdac_0'
    folder = 'K:\\data\\FE-I4\\ChargeRecoMethod\\'
    
    chip_flavor = 'fei4a'
    input_file_hits = folder + scan_name + "_interpreted.h5"
    output_file_hits = folder + scan_name + "_cut_3.h5"
    scan_data_filename = folder + scan_name

    use_cluster_rate_correction = False

    gdac_range = range(100, 114, 1)  # the GDAC range used during the calibration
    gdac_range.extend((np.exp(np.array(range(0, 150)) / 10.) / 10. + 100).astype('<u8')[50:-40].tolist())  # exponential GDAC range to correct for logarithmic threshold(GDAC) function

    

    with tb.openFile(input_file_calibration, mode="r") as in_file_calibration_h5:  # read calibration file from calibrate_threshold_gdac scan
        with tb.openFile(input_file_hits, mode="r") as in_file_hits_h5:  # read scan data file from scan_fei4_trigger_gdac scan
            hits = in_file_hits_h5.root.HistOcc[:]
            mean_threshold_calibration = in_file_calibration_h5.root.MeanThresholdCalibration[:]
            threshold_calibration_table = in_file_calibration_h5.root.ThresholdCalibration[:]
            threshold_calibration_array = in_file_calibration_h5.root.HistThresholdCalibration[:]

            gdac_range_calibration = gdac_range
            gdac_range_source_scan = analysis_utils.get_scan_parameter(meta_data_array=in_file_hits_h5.root.meta_data[:])['GDAC']

            correction_factors = 1
            if use_cluster_rate_correction:
                correction_h5 = tb.openFile(input_file_correction, mode="r")
                cluster_size_histogram = correction_h5.root.AllHistClusterSize[:]
                correction_factors = get_hit_rate_correction(gdacs=gdac_range_source_scan[:-1], calibration_gdacs=gdac_range_source_scan, cluster_size_histogram=cluster_size_histogram)

            logging.info('Analyzing source scan data with %d different GDAC settings from %d to %d with minimum step sizes from %d to %d' % (len(gdac_range_source_scan), np.min(gdac_range_source_scan), np.max(gdac_range_source_scan), np.min(np.gradient(gdac_range_source_scan)), np.max(np.gradient(gdac_range_source_scan))))
            logging.info('Use calibration data with %d different GDAC settings from %d to %d with minimum step sizes from %d to %d' % (len(gdac_range_calibration), np.min(gdac_range_calibration), np.max(gdac_range_calibration), np.min(np.gradient(gdac_range_calibration)), np.max(np.gradient(gdac_range_calibration))))

            pixel_thresholds = get_pixel_thresholds(gdacs=gdac_range_source_scan, calibration_gdacs=gdac_range_calibration, threshold_calibration_array=threshold_calibration_array)  # interpolates the threshold at the source scan GDAC setting from the calibration
            pixel_hits = np.swapaxes(hits, 0, 1)  # create hit array with shape (col, row, ...)

            pixel_thresholds = pixel_thresholds[:, :, :-1]
            pixel_hits = pixel_hits[:, :, :-1]

            pixel_hits = pixel_hits * correction_factors

            # choose good region
            selected_pixel_thresholds = pixel_thresholds[33:37, 190:215, :]
            selected_pixel_hits = pixel_hits[33:37, 190:215, :]

            # reshape to one dimension
            x = np.reshape(selected_pixel_thresholds, newshape=(selected_pixel_thresholds.shape[0] * selected_pixel_thresholds.shape[1], selected_pixel_thresholds.shape[2])).ravel()
            y = np.reshape(selected_pixel_hits, newshape=(selected_pixel_hits.shape[0] * selected_pixel_hits.shape[1], selected_pixel_hits.shape[2])).ravel()

            #nothing should be NAN, NAN is not supported yet
            if np.isnan(x).sum() > 0 or np.isnan(y).sum() > 0:
                logging.warning('There are pixels with NaN threshold or hit values, analysis will be wrong')

            plot_profile_histogram(x=x * 55., y=y / 100., n_bins=len(gdac_range_source_scan) / 2, title='Triple hit cluster rate for different pixel thresholds', x_label='pixel threshold [e]', y_label='triple hit cluster rate [1/s]')

#             x = get_mean_threshold(gdac_range_source_scan, mean_threshold_calibration)
#             y = selected_pixel_hits.mean(axis=(0, 1))
# 
#             plot_scatter(x * 55, y, title='Mean single pixel cluster rate at different thresholds', x_label='mean threshold [e]', y_label='mean single pixel cluster')

    if use_cluster_rate_correction:
        correction_h5.close()
