from matplotlib import use 
use('Agg')
import numpy as np
import xarray as xr
import pandas as pd
import time
import argparse
import os
import xarray as xr
import tensorflow as tf
import glob
import matplotlib.pyplot as plt
import cmweather
import paramiko

#from waggle.plugin import Plugin
from datetime import datetime, timedelta
from scipy.signal import convolve2d
from tensorflow.keras.models import load_model
from tensorflow.keras.preprocessing.image import ImageDataGenerator
from tensorflow.keras.applications.resnet50 import preprocess_input

from glob import glob
# 1. import standard logging module
import logging
import utils

# 2. enable debug logging
lidar_ip_addr = '192.168.1.90'
lidar_uname = 'waggle'
lidar_pwd = 'w8ggl3'
#logging.basicConfig(level=logging.DEBUG)

def convert_to_hours_minutes_seconds(decimal_hour, initial_time):
    delta = timedelta(hours=decimal_hour)
    return datetime(initial_time.year, initial_time.month, initial_time.day) + delta

def load_file(file):
    field_dict = utils.hpl2dict(file)
    initial_time = pd.to_datetime(field_dict['start_time'])
    
    time = pd.to_datetime([convert_to_hours_minutes_seconds(x, initial_time) 
        for x in field_dict['decimal_time']])

    ds = xr.Dataset(coords={'range': field_dict['center_of_gates'],
                            'time': time,
                            'azimuth': ('time', field_dict['azimuth'])},
                    data_vars={'radial_velocity':(['time', 'range'],
                                                  field_dict['radial_velocity'].T),
                               'beta': (['time', 'range'],
                                        field_dict['beta'].T),
                               'intensity': (['time', 'range'],
                                             field_dict['intensity'].T)
                              }
                   )
    ds['snr'] = 10 * np.log10(ds['intensity'] - 1)
    return ds


def return_convolution_matrix(time_window, range_window):
    return np.ones((time_window, range_window)) / (time_window * range_window)

def make_imgs(ds, config, interval=5):
    range_bins = np.arange(60., 11280., 120.)
    # Parse model string for locations of snr, mean_velocity, spectral_width
    locs = 0
    snr_thresholds = []
    scp_ds = {}
    interval = 5
    dates = pd.date_range(ds.time.values[0], ds.time.values[-1], freq='%dmin' % interval)
    
    times = ds.time.values
    print(times)
    which_ranges = int(np.argwhere(ds.range.values < 8000.)[-1])
    ranges = np.tile(ds.range.values, (ds['snr'].shape[0], 1))
    conv_matrix = return_convolution_matrix(5, 5)
    ds['snr'] = ds['snr'] + 2 * np.log10(ranges + 1)
    snr_avg = convolve2d(ds['snr'].values, conv_matrix, mode='same') 
    ds['stddev'] = (('time', 'range'), np.sqrt(convolve2d((ds['snr'] - snr_avg)**2, conv_matrix, mode='same')))
    ds['stddev'] = ds['stddev'].fillna(0)

    Zn = ds.stddev.values

    cur_time = times[0]
    end_time = times[-1]
    time_list = []
    start_ind = 0
    i = 0
    first_shape = None

    while cur_time < end_time:
        next_time = cur_time + np.timedelta64(interval, 'm')
        print((next_time, end_time))

        if next_time > end_time:
            next_ind = len(times)
        else:
            next_ind = np.argmin(np.abs(next_time - times))
        if (start_ind >= next_ind):
            break

        my_data = Zn[start_ind:next_ind, 0:which_ranges].T

        my_times = times[start_ind:next_ind]
        if len(my_times) == 0:
            break
        start_ind += next_ind - start_ind + 1

        if first_shape is None:
            first_shape = my_data.shape
        else:
            if my_data.shape[0] > first_shape[0]:
                my_data = my_data[:first_shape[0], :]
            elif my_data.shape[0] < first_shape[0]:
                my_data = np.pad(my_data, [(0, first_shape[0] - my_data.shape[0]), (0, 0)],
                                 mode='constant')
        if not os.path.exists('imgs'):
            os.mkdir('imgs')
        
        if not os.path.exists('imgs/train'):
            os.mkdir('imgs/train')

        fname = 'imgs/train/%d.png' % i
        width = first_shape[0]
        height = first_shape[1]
        
        # norm = norm.SerializeToStri
        fig, ax = plt.subplots(1, 1, figsize=(1, 1 * (height/width)))
        # ax.imshow(my_data)
        ax.pcolormesh(my_data, cmap='HomeyerRainbow', vmin=0, vmax=5)
        ax.set_axis_off()
        ax.margins(0, 0)
        try:
            fig.savefig(fname, dpi=300, bbox_inches='tight', pad_inches=0)
        except RuntimeError:
            plt.close(fig)
            continue

        plt.close(fig)
        i = i + 1
        del fig, ax
        time_list.append(cur_time)
        cur_time = next_time

    return time_list


def progress(bytes_so_far: int, total_bytes: int):
    pct_complete = 100. * float(bytes_so_far) / float(total_bytes)
    if int(pct_complete * 10) % 100 == 0:
        print("Total progress = %4.2f" % pct_complete)


def worker_main(args):
    logging.debug("Loading model %s" % args.model)
    model = load_model(args.model)
    interval = int(args.interval)
    logging.debug('opening input %s' % args.input)
   
    old_file = ""
    run = True
    already_done = []
    if args.date is None:
        cur_date = datetime.now().strftime("%Y%m%d")
    else:
        cur_date = args.date
 #   with Plugin() as plugin:
    while run:
        class_names = ['clear', 'cloudy']
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(lidar_ip_addr, username=lidar_uname, password=lidar_pwd)
            print("Connected to the Lidar!")

            with ssh.open_sftp() as sftp:
                remote_dir = '/C:/Lidar/Data/Proc/%s/%s/%s' % (
                    cur_date[:4], cur_date[:6], cur_date)
                file_list = sftp.listdir(remote_dir)
                stare_list = []
                for fi in file_list:
                    if "Stare" in fi:
                        print(fi)
                        if not os.path.exists('stares'):
                            os.makedirs('stares')
                        base, name = os.path.split(fi)
                        sftp.get(os.path.join(remote_dir, fi), 
                                 os.path.join(os.path.join(
                            os.getcwd(), 'stares'), name))
                        stare_list.append(os.path.join(os.path.join(
                            os.getcwd(), 'stares'), name))
                        
        
                for fi in stare_list:
                    logging.debug("Processing %s" % fi)
                    dsd_ds = load_file(fi)
                    print(dsd_ds)
                    time_list = make_imgs(dsd_ds, args.config)
                    dsd_ds.close()
                    file_list = glob('imgs/*.png')
                    print(file_list)
                    
                    img_gen = ImageDataGenerator(
                        preprocessing_function=preprocess_input)

                    gen = img_gen.flow_from_directory(
                            'imgs/', target_size=(256, 128),
                            shuffle=False)
                    out_predict = model.predict(gen).argmax(axis=1)
                    num_clouds = 0
                    for i, ti in enumerate(time_list):
                        if ti not in already_done:
                            tstamp = int(ti)
                            
                            if out_predict[i] == 0:
                                string = "clear"
                            else:
                                string = "clouds/rain"
                                num_clouds += 1
                            print("%s: %s" % (str(ti), string))
                            
                            #plugin.publish("weather.classifier.class",
                            #        int(out_predict[i]),
                            #        timestamp=tstamp)
                            already_done.append(ti)
                    if num_clouds < 6:
                        time_str = str(time_list[0])[:9]
                        print("Hour %s is mostly clear, deleting." % time_str)
                        remote_dir = '/C:/Lidar/Data/Raw/%s/%s/%s' % (
                    cur_date[:4], cur_date[:6], cur_date)
                        file_list = sftp.listdir(remote_dir)
                        for fi in file_list:
                            time_str = str(time_list[0])[:9]
                            time_str = time_str[:4] + "_" + time_str[6:8]
                            if time_str in fi:
                                try:
                                    sftp.remove(os.path.join(remote_dir, fi))
                                except:
                                    continue
                    else:
                        time_str = str(time_list[0])
                        print("Hour %s contains mostly clouds and rain, preserving data." % time_str)

            if args.loop == False:
                run = False


def main(args):
    if args.verbose:
        print('running in a verbose mode')
    worker_main(args)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--verbose', dest='verbose',
        action='store_true', help='Verbose')
    parser.add_argument(
        '--input', dest='input',
        action='store', default='/data',
        help='Path to input device or ARM datastream name')
    parser.add_argument(
        '--model', dest='model',
        action='store', default='resnet50.hdf5',
        help='Path to model')
    parser.add_argument(
        '--interval', dest='interval',
        action='store', default=0,
        help='Time interval in seconds')
    parser.add_argument(
            '--loop', action='store_true')
    parser.add_argument(
            '--no-download',
            help='Do not Download from lidar', dest='download',
            action='store_false')
    parser.add_argument(
            '--delete-clear',
            help='Delete clear hours from lidar', dest='del_clear',
            action='store_true'
    )
    parser.add_argument(
            '--delete-cloudy',
            help='Delete cloud/rain hours from lidar', dest='del_cloud',
    )
    parser.add_argument('--no-loop', action='store_false')
    parser.set_defaults(loop=True)
    parser.add_argument(
            '--config', dest='config', action='store', default='dlacf',
            help='Set to User5 for PPI or Stare for VPTs')
    parser.add_argument('--date', dest='date', action='store',
                        default=None,
                        help='Date of record to pull in (YYYY-MM-DD)')
    parser.add_argument('--time', dest='time', action='store',
                        default=None, help='Time of record to pull')

    gpus = tf.config.experimental.list_physical_devices('GPU')
    if gpus:
        try:
            tf.config.experimental.set_virtual_device_configuration(
                    gpus[0], [tf.config.experimental.VirtualDeviceConfiguration(memory_limit=1024)])
        except RuntimeError as e:
            print(e)
    main(parser.parse_args())
                                            
