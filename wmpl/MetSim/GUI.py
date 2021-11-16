""" GUI interface to meteor ablation models which enables manual modelling of meteors. """


import os
import sys
import copy
import argparse
import time
import json
import glob
import multiprocessing as mp

import numpy as np
import scipy.stats
import scipy.interpolate
import scipy.optimize
import scipy.signal
import matplotlib.pyplot as plt

from PyQt5.QtWidgets import QApplication, QMainWindow, QMessageBox, QFileDialog
from PyQt5.uic import loadUi

from wmpl.Formats.Met import loadMet
from wmpl.MetSim.GUITools import MatplotlibPopupWindow
from wmpl.MetSim.MetSimErosion import runSimulation, Constants
from wmpl.MetSim.MetSimErosionCyTools import luminousEfficiency, massLossRK4, decelerationRK4
from wmpl.Trajectory.Orbit import calcOrbit
from wmpl.Trajectory.Trajectory import Trajectory, ObservedPoints, PlaneIntersection
from wmpl.Utils.AtmosphereDensity import fitAtmPoly, getAtmDensity, atmDensPoly
from wmpl.Utils.Math import mergeClosePoints, findClosestPoints, vectMag, lineFunc, meanAngle
from wmpl.Utils.Physics import calcMass, dynamicPressure
from wmpl.Utils.Pickling import loadPickle, savePickle
from wmpl.Utils.Plotting import saveImage
from wmpl.Utils.TrajConversions import unixTime2JD, geo2Cartesian, cartesian2Geo, altAz2RADec, \
    altAz2RADec_vect, raDec2ECI




### CONSTANTS ###

# Height padding for top plots
BEG_HT_PAD = +5
END_HT_PAD = -2

# Text label height padding
TEXT_LABEL_HT_PAD = 0.1

# Legend text size
LEGEND_TEXT_SIZE  = 8

### ###

class FragEntryRes:
    def __init__(self, name):
        self.name = name
        self.t_list = []
        self.m_list = []
        self.v_list = []
        self.h_list = []
        self.tau_list = []

def calcLumEff(frag_type, h, m, v, t, f_total=None, comp_type=1, sigma=0.03, shape_factor=1, gamma=1, bulk_dens=3500, const_tau=1, \
                            min_height=0, max_height=100000, bin_size=100, dens_const=None):

    """ calcLumEff - Returns luminous efficiency as a function of height, mass, time, and velocity

    frag_type [String] - name of type of fragmentation (main, leading, grain)
    h [array] - array of heights (descending)
    m [float] - initial mass of object OR [array] - masses corresponding with heights
    v [float] - initial velocity of object OR [array] - velocities corresponding with heights
    t [array] - array of times corresponding with height
    
    note m and v both given as floats OR arrays. If both given as arrays, then sigma, shape_factor, 
    bulk density do not need to be given
    
    comp_type [int] - composition type corresponding to model in luminousEfficiency()
    f_total [FragEntryRes obj] - object containing running total of tau of all objects

    Meteoroid parameters, only required if m and v are given as a float
    sigma [float] - Ablation coefficient (s^2/m^2).    
    shape_factor [float] - Shape Factor
    gamma [float]  - Gamma
    bulk_dens [float] - Bulk Density kg/m^2

    const_tau [float] - Value of the constant luminous efficiency (percent)

    min_height, max_height [float] - height boundaries for binning luminous efficiencies
    bin_size [float] - bin of heights for considering mass loss of multiple objects
    dens_const [ndarray] - array of atmospheric density polynomial coefficients

    ########### Returns:
    f [FragEntryRes Obj] - details of the object in question
    f_total [FragEntryRes] - Running total of all objects
    """

    # check if both v and m are arrays
    vm_is_array = not (isinstance(m, float) and isinstance(v, float))

    # Length of each bin to consider for total mass loss
    BIN_SIZE = bin_size
    HEIGHT_MAX = max_height
    HEIGHT_MIN = min_height

    # Store a running total of mass for plotting
    if f_total is None:
        f_total = FragEntryRes("Total")
        f_total.tau_list =  [1e-6]*((HEIGHT_MAX - HEIGHT_MIN)//BIN_SIZE + 1)
        f_total.t_list =    [1e-6]*((HEIGHT_MAX - HEIGHT_MIN)//BIN_SIZE + 1)
        f_total.m_list =    [1e-6]*((HEIGHT_MAX - HEIGHT_MIN)//BIN_SIZE + 1)
        f_total.v_list =    [1e-6]*((HEIGHT_MAX - HEIGHT_MIN)//BIN_SIZE + 1)
        f_total.h_list =    [1e-6]*((HEIGHT_MAX - HEIGHT_MIN)//BIN_SIZE + 1)


    # Create new object
    f = FragEntryRes(frag_type)

    # Shape-density coefficient (m^2/kg^(2/3)).
    K = shape_factor*gamma*bulk_dens**(-2/3)

    # derivative loop
    for i in range(len(h) - 1):

        if h[i] > HEIGHT_MAX or h[i] < HEIGHT_MIN:
            continue

        dt = t[i + 1] - t[i]
        dh = h[i + 1] - h[i]

        # Mass loss
        if vm_is_array:
            dm = m[i + 1] - m[i]
            dv = v[i + 1] - v[i]
        else:
            rho_atm = atmDensPoly(h[i] + dh/2, dens_const)
            dm = massLossRK4(dt, K, sigma, m, rho_atm, v)
            dv = decelerationRK4(dt, K, m, rho_atm, v)

            m += dm*dt
            v += dv*dt

        f.t_list.append(t[i] + dt/2)
        f.h_list.append(h[i] + dh/2)

        if vm_is_array:
            f.m_list.append(m[i])
            f.v_list.append(v[i])
        else:
            f.m_list.append(m)
            f.v_list.append(v)

        indx = int((h[i])/(HEIGHT_MAX - HEIGHT_MIN)*len(f_total.h_list))

        f_total.m_list[indx] += dm
        f_total.h_list[indx] = h[i]

        if vm_is_array:
            tau = luminousEfficiency(comp_type, const_tau, v[i], m[i])
            f_total.v_list[indx] = v[i]
        else:
            tau = luminousEfficiency(comp_type, const_tau, v, m)
            f_total.v_list[indx] = v


        f_total.tau_list[indx] += tau*dm

        f.tau_list.append(tau)

    return f, f_total

def plotLumEff(frag_list):
    """
    Plots the luminous efficiency with height for a list of FragEntryRes objects

    # Arguments
    frag_list [list] - list of FragEntryRes objects, which have frag_types of "Main", "Grain", "Total", or "Leading"
    """

    main_plotted = False
    for f in frag_list:

        if f.name == "Main":

            # Only plot the label once
            if main_plotted:
                plt.plot(np.array(f.h_list)/1000, np.array(f.tau_list)*100, c='r')
            else:
                main_plotted = True
                plt.plot(np.array(f.h_list)/1000, np.array(f.tau_list)*100, label=f.name, c='r')

        elif f.name == "Grain":

            plt.plot(np.array(f.h_list)/1000, np.array(f.tau_list)*100, label=f.name, c='c')

        elif f.name == "Total":

            order = np.argsort(f.h_list)
            mass_weight = (np.array(f.tau_list)/np.array(f.m_list))*100
            h = np.array(f.h_list)
            plt.plot((h/1000)[order], (mass_weight)[order], label=f.name, c='k')

        elif f.name == "Leading":

            plt.plot(np.array(f.h_list)/1000, np.array(f.tau_list)*100, label=f.name, c='g')

    plt.legend()
    plt.xlabel("Height [km]")
    plt.ylabel("Luminous Efficiency [%]")
    plt.ylim([0, 20])
    plt.xlim([17, 80])

    plt.show()



class SimulationResults(object):
    def __init__(self, const, frag_main, results_list, wake_results):
        """ Container for simulation results. """

        # Final physical parametrs of the main fragment
        self.frag_main = frag_main

        # Unpack the results
        results_list = np.array(results_list).astype(np.float64)
        self.time_arr, self.luminosity_arr, self.luminosity_main_arr, self.luminosity_eroded_arr, \
            self.electron_density_total_arr, self.brightest_height_arr, self.brightest_length_arr, \
            self.brightest_vel_arr, self.leading_frag_height_arr, self.leading_frag_length_arr, \
            self.leading_frag_vel_arr, self.mass_total_active_arr, self.main_mass_arr, \
            self.main_height_arr, self.main_length_arr, self.main_vel_arr = results_list.T


        # Calculate the total absolute magnitude (apparent @100km), and fix possible NaN values (replace them 
        #   with the faintest magnitude)
        self.abs_magnitude = -2.5*np.log10(self.luminosity_arr/const.P_0m)
        self.abs_magnitude[np.isnan(self.abs_magnitude)] = np.nanmax(self.abs_magnitude)

        # Compute the absolute magnitude of the main fragment
        self.abs_magnitude_main = -2.5*np.log10(self.luminosity_main_arr/const.P_0m)
        self.abs_magnitude_main[np.isnan(self.abs_magnitude_main)] = np.nanmax(self.abs_magnitude_main)

        # Compute the absolute magnitude of the eroded and disruped grains
        self.abs_magnitude_eroded = -2.5*np.log10(self.luminosity_eroded_arr/const.P_0m)
        self.abs_magnitude_eroded[np.isnan(self.abs_magnitude_eroded)] = np.nanmax(self.abs_magnitude_eroded)   


        # Interpolate time vs leading fragment height
        leading_frag_ht_interpol = scipy.interpolate.interp1d(self.time_arr, self.leading_frag_height_arr)

        # Compute the absolute magnitude of individual fragmentation entries, and join them a height of the
        #   leading fragment
        if const.fragmentation_show_individual_lcs:
            self.frag_list = []

            f, f_total = calcLumEff("Leading", self.leading_frag_height_arr, const.m_init, const.v_init, self.time_arr, f_total=None, \
                  comp_type=const.lum_eff_type, sigma=const.sigma, shape_factor=const.shape_factor, gamma=const.gamma, bulk_dens=const.rho,\
                                    const_tau=const.lum_eff, dens_const=const.dens_co)


            self.frag_list.append(f)

            for frag_entry in const.fragmentation_entries:

                # Compute values for the main fragment
                if len(frag_entry.main_time_data):

                    # Find the corresponding height for every time
                    frag_entry.main_height_data = leading_frag_ht_interpol(np.array(frag_entry.main_time_data))

                    # Compute the magnitude
                    frag_entry.main_abs_mag = -2.5*np.log10(np.array(frag_entry.main_luminosity)/const.P_0m)

                    # Store luminous efficincies of fragment (f), and total (f_total)
                    f, f_total = calcLumEff("Main", frag_entry.main_height_data, frag_entry.mass, frag_entry.velocity, frag_entry.main_time_data, f_total=f_total, \
                                    comp_type=const.lum_eff_type, sigma=frag_entry.sigma, shape_factor=const.shape_factor, gamma=const.gamma, bulk_dens=const.rho,\
                                    const_tau=const.lum_eff, dens_const=const.dens_co)


                    self.frag_list.append(f)

                # Compute values for the grains
                if len(frag_entry.grains_time_data):

                    # Find the corresponding height for every time
                    frag_entry.grains_height_data = leading_frag_ht_interpol(np.array(frag_entry.grains_time_data))

                    # Compute the magnitude
                    frag_entry.grains_abs_mag = -2.5*np.log10(np.array(frag_entry.grains_luminosity)/const.P_0m)

                    # Store luminous efficincies of fragment (f), and total (f_total)
                    f, f_total = calcLumEff("Grain", frag_entry.main_height_data, frag_entry.mass, frag_entry.velocity, frag_entry.main_time_data, f_total=f_total, \
                                    comp_type=const.lum_eff_type, sigma=frag_entry.sigma, shape_factor=const.shape_factor, gamma=const.gamma, bulk_dens=const.rho, \
                                    const_tau=const.lum_eff, dens_const=const.dens_co)

                   

                    self.frag_list.append(f)


            self.frag_list.append(f_total)

            # Plotting Function
            # plotLumEff(self.frag_list)
            

        ### Wake simulation ###

        self.wake_results = wake_results
        self.wake_max_lum = 0

        if np.any(wake_results):
            
            # Determine the wake plot upper limit
            self.wake_max_lum = max([max(wake.wake_luminosity_profile) for wake in wake_results \
                if wake is not None])


        ###


class MetObservations(object):
    def __init__(self, met, traj):
        """ Container for additional observations from a .met file. Computes the lag and apparent magnitude
            given the base trajectory solution. 
        """

        self.met = met
        self.traj = traj

        self.sites = [site for site in met.sites]


        self.time_data = {}
        self.height_data = {}
        self.lag_data = {}
        self.abs_mag_data = {}


        # Compute the lag and the magnitude using the given observations
        if self.met is not None:

            # Project additional observations from the .met file to the trajectory and compute the lag
            # Go through all sites
            for site in met.sites:

                jd_picks = []
                time_rel_picks = []
                theta_picks = []
                phi_picks = []
                mag_picks = []

                # Go through all picks
                for pick in met.picks_objs[site]:

                    # Add the pick to the picks list
                    theta_picks.append(pick.theta)
                    phi_picks.append(pick.phi)

                    # Convert the reference Unix time to Julian date
                    ts = int(pick.unix_time)
                    tu = (pick.unix_time - ts)*1e6
                    jd = unixTime2JD(ts, tu)

                    # Add the time of the pick to a list
                    jd_picks.append(jd)

                    # Compute relative time in seconds since the beginning of the meteor
                    time_rel_picks.append((jd - self.traj.jdt_ref)*86400)

                    # Add magnitude
                    mag_picks.append(pick.mag)


                jd_picks = np.array(jd_picks).ravel()
                time_rel_picks = np.array(time_rel_picks).ravel()
                theta_picks = np.array(theta_picks).ravel()
                phi_picks = np.array(phi_picks).ravel()
                mag_picks = np.array(mag_picks).ravel()


                # Compute RA/Dec of observations
                ra_picks, dec_picks = altAz2RADec_vect(np.pi/2 - phi_picks, np.pi/2 - theta_picks, \
                    jd_picks, met.lat[site], met.lon[site])

                
                # List of distances from the state vector (m)
                state_vect_dist = []

                # List of heights (m)
                height_data = []

                # List of absolute magnitudes
                abs_mag_data = []

                # Project rays to the trajectory line
                for i, (jd, ra, dec, mag) in enumerate(np.c_[jd_picks, ra_picks, dec_picks, mag_picks]):

                    # Compute the station coordinates at the given time
                    stat = geo2Cartesian(met.lat[site], met.lon[site], met.elev[site], jd)

                    # Compute measurement rays in cartesian coordinates
                    meas = np.array(raDec2ECI(ra, dec))


                    # Calculate closest points of approach (observed line of sight to radiant line)
                    obs_cpa, rad_cpa, d = findClosestPoints(stat, meas, self.traj.state_vect_mini, \
                        self.traj.radiant_eci_mini)

                    # Compute the height (meters)
                    _, _, ht = cartesian2Geo(jd, *rad_cpa)
                    height_data.append(ht)


                    # If the height is higher than the beginning height, make the state vector distance 
                    #   negative
                    state_vect_dist_sign = 1
                    if ht > self.traj.rbeg_ele:
                        state_vect_dist_sign = -1

                    # Distance from the state vector to the projected point on the radiant line
                    state_vect_dist.append(state_vect_dist_sign*vectMag(self.traj.state_vect_mini - rad_cpa))

                    # Compute the range to the station and the absolute magnitude
                    if mag is not None:
                        r = vectMag(rad_cpa - stat)
                        abs_mag = mag + 5*np.log10(100000/r)
                    else:
                        abs_mag = np.nan

                    abs_mag_data.append(abs_mag)


                state_vect_dist = np.array(state_vect_dist)


                # Compute the lag
                self.lag_data[site] = state_vect_dist - lineFunc(time_rel_picks, *self.traj.velocity_fit)

                # Store the time, heights, magnitudes
                self.time_data[site] = np.array(time_rel_picks)
                self.height_data[site] = np.array(height_data)
                self.abs_mag_data[site] = np.array(abs_mag_data)
                


class LightCurveContainer(object):
    def __init__(self, dir_path, file_name):
        """ Loads the light curve data form an CSV file. 
        Arguments:
            dir_path: [str] Path to the directory with the LC file.
            file_name: [str] Name of the LC CSV file.
        """

        self.sites = []

        self.time_data = {}
        self.height_data = {}
        self.abs_mag_data = {}

        with open(os.path.join(dir_path, file_name)) as f:
            
            time_data = []
            height_data = []
            abs_mag_data = []
            current_station = None

            station_label = "# Station:"

            for line in f:

                line = line.replace('\n', '').replace('\r', '')

                if not len(line):
                    continue

                # Start a new station
                if line.startswith(station_label):

                    # Add the previous data to the dictionary
                    if current_station is not None:
                        self.sites.append(current_station)
                        print("Loaded {:d} mag points from station: {:s}".format(len(time_data), current_station))
                        self.time_data[current_station] = np.array(time_data)
                        self.height_data[current_station] = np.array(height_data)
                        self.abs_mag_data[current_station] = np.array(abs_mag_data)

                    time_data = []
                    height_data = []
                    abs_mag_data = []
                    current_station = line.strip(station_label).strip().replace(',', '')

                    continue


                # Skip comments
                if line.startswith('#'):
                    continue

                # Read the data line (convert height to meters)
                t, ht, mag = line.split(',')
                time_data.append(float(t))
                height_data.append(1000*float(ht))
                abs_mag_data.append(float(mag))


            # Add the final station to the list
            if current_station is not None:
                print("Loaded {:d} mag points from station: {:s}".format(len(time_data), current_station))
                self.sites.append(current_station)
                self.time_data[current_station] = np.array(time_data)
                self.height_data[current_station] = np.array(height_data)
                self.abs_mag_data[current_station] = np.array(abs_mag_data)



class WakePoint(object):
    def __init__(self, n, th, phi, intens_sum, amp, r, b, c, state_vect_dist, ht):
        """ Container for wake points. """

        # Distance along the trail (pixels)
        self.n = n

        # Zenith angle (deg)
        self.th = th

        # Azimuth +N of due E (deeg)
        self.phi = phi

        # Intensity sum
        self.intens_sum = intens_sum

        # Peak pixel intensity
        self.amp = amp

        # Raw trail width (pixels above the background)
        self.r = r

        # Standard deviation of the Gaussian fit (meters)
        self.b = b

        # Corrected trail width (raw - stellar width) (meters)
        self.c = c

        # Distance from the trajectory state vector (m)
        self.state_vect_dist = state_vect_dist

        # Height (m)
        self.ht = ht

        # Distance from the leading fragment (m)
        self.leading_frag_length = 0



class WakeContainter(object):
    def __init__(self, site_id, frame_n):
        """ Containter for wake profile data extracted from mirfit wid files. 
        
        Arguments:
            site_id: [str] Name of the site.
            frame_n: [str] Frame number of the measurement.
        """

        self.site_id = int(site_id)
        self.frame_n = frame_n

        self.points = []


    def addPoint(self, n, th, phi, intens_sum, amp, r, b, c, state_vect_dist, ht):
        self.points.append(WakePoint(n, th, phi, intens_sum, amp, r, b, c, state_vect_dist, ht))




class FragmentationEntry(object):
    def __init__(self, frag_type, height, number, mass_percent, sigma, gamma, erosion_coeff, grain_mass_min, \
        grain_mass_max, mass_index, normalize_units=False):
        """ A container for every fragmentation entry. 

        normalize_units: [bool] Convert the units of sigma and erosion coeff from s^2 km^-2 to s^2 m^-2,
            and convert the height to meters from km.
        """

        norm = 0.0
        if normalize_units:
            norm = 1.0


        ### STATUS FLAGS ###


        # Unique identifer of the fragmentation entry, will be assigned at the beginning of simulation
        self.id = None

        # Indicates that the fragmentation was not performed yet
        self.done = False


        ### ###

        ### INPUT parameters ###


        self.frag_type = frag_type.strip()

        self.height = (1000**norm)*float(height)

        try:
            self.number = int(number)
        except:
            if (self.frag_type == "F") or (self.frag_type == "EF"):
                self.number = 1
            else:
                self.number = None

        try:
            self.mass_percent = float(mass_percent)

            # Limit mass loss to 100%
            if self.mass_percent > 100:
                mass_percent = 100.0

        except:
            self.mass_percent = None

        try:
            self.sigma = float(sigma)/(1e6**norm)
        except:
            self.sigma = None

        try:
            self.gamma = float(gamma)
        except:
            self.gamma = None

        try:
            self.erosion_coeff = float(erosion_coeff)/(1e6**norm)
        except:
            self.erosion_coeff = None

        try:
            self.grain_mass_min = float(grain_mass_min)
        except:
            self.grain_mass_min = None

        try:
            self.grain_mass_max = float(grain_mass_max)
        except:
            self.grain_mass_max = None

        try:
            self.mass_index = float(mass_index)
        except:
            self.mass_index = None

        ### ###


        ### List of fragments generated by the fragmentation ###

        self.fragments = []

        ###


        ### Output parameters ###

        # Time of fragmentation
        self.time = None

        # Dynamic pressure of fragmentation
        self.dyn_pressure = None

        # Velocity of fragmentation
        self.velocity = None

        # Parent mass at the moment of fragmentation
        self.parent_mass = None

        # Initial mass of the fragments
        self.mass = None

        # Final mass of the fragments
        self.final_mass = None


        self.resetOutputParameters()
        

        ### ###


    def resetOutputParameters(self):
        """ Reset lists with output parameters. """

        # Time data of the main parent fragment (not the grains)
        self.main_time_data = []

        # Height (will be joined in postprocessing to the height of the leading fragment)
        self.main_height_data = []

        # Luminosity of the main parent
        self.main_luminosity = []

        # Absolute magnitude (will be computed after the simulation)
        self.main_abs_mag = []



        # Time data for the grains
        self.grains_time_data = []

        # Height (will be joined in postprocessing to the height of the leading fragment)
        self.grains_height_data = []

        # Luminosity of the grains
        self.grains_luminosity = []

        # Absolute magnitude (will be computed after the simulation)
        self.grains_abs_mag = []


    def toString(self):
        """ Convert the entry to a string that can be written to a text file. """

        line_entries = []

        line_entries.append("{:>6s}".format(self.frag_type))

        line_entries.append("{:11.3f}".format(self.height/1000))

        if self.number is not None:
            line_entries.append("{:6d}".format(self.number))
        else:
            line_entries.append(6*" ")

        if self.mass_percent is not None:
            line_entries.append("{:8.3f}".format(self.mass_percent))
        else:
            line_entries.append(8*" ")

        if self.sigma is not None:
            line_entries.append("{:14.4f}".format(1e6*self.sigma))
        else:
            line_entries.append(14*" ")

        if self.gamma is not None:
            line_entries.append("{:5.2f}".format(self.gamma))
        else:
            line_entries.append(5*" ")

        if self.erosion_coeff is not None:
            line_entries.append("{:13.4f}".format(1e6*self.erosion_coeff))
        else:
            line_entries.append(13*" ")

        if self.grain_mass_min is not None:
            line_entries.append("{:9.2e}".format(self.grain_mass_min))
        else:
            line_entries.append(9*" ")

        if self.grain_mass_max is not None:
            line_entries.append("{:9.2e}".format(self.grain_mass_max))
        else:
            line_entries.append(9*" ")

        if self.mass_index is not None:
            line_entries.append("{:5.2f}".format(self.mass_index))
        else:
            line_entries.append(5*" ")


        # Join the entries to one string
        out_str = ", ".join(line_entries)

        # Add separator from inputs and outputs
        out_str += " # "


        line_entries = []

        if self.time is not None:
            line_entries.append("{:9.6f}".format(self.time))
        else:
            line_entries.append(9*" ")

        if self.dyn_pressure is not None:
            line_entries.append("{:9.3f}".format(self.dyn_pressure/1000))
        else:
            line_entries.append(9*" ")

        if self.velocity is not None:
            line_entries.append("{:8.3f}".format(self.velocity/1000))
        else:
            line_entries.append(8*" ")

        if self.parent_mass is not None:
            line_entries.append("{:11.2e}".format(self.parent_mass))
        else:
            line_entries.append(11*" ")

        if self.mass is not None:
            line_entries.append("{:9.2e}".format(self.mass))
        else:
            line_entries.append(9*" ")

        if self.final_mass is not None:
            line_entries.append("{:10.2e}".format(self.final_mass))
        else:
            line_entries.append(10*" ")

        out_str += ", ".join(line_entries)

        # Add final separator
        out_str += " #"


        return out_str



            



class FragmentationContainer(object):
    def __init__(self, gui, fragmentation_file_path):
        """ Class which handles fragmentation file I/O. 
    
        Arguments:
            gui: [MetSimGUI object] A handle to the parent GUI object.
            fragmentation_file_path: [str] Path to the main fragmentation file.

        """

        self.gui = gui

        self.fragmentation_file_path = fragmentation_file_path

        self.fragmentation_entries = []


    def sortByHeight(self):
        """ Sort the fragmentation entries by height, from highest to lowest. """

        self.fragmentation_entries = sorted(self.fragmentation_entries, key=lambda x: x.height, reverse=True)


    def resetAll(self):
        """ Reset the 'done' flag of all fragments to False, which means that the fragmentation should run.
        """

        for frag_entry in self.fragmentation_entries:
            frag_entry.done = False


    def loadFromString(self, string):
        """ Load parameters from a string. """

        # Reset fragmentation entries and reload
        self.fragmentation_entries = []

        for line in string.split('\n'):

            # Skip comment lines
            if line.startswith("#"):
                continue

            line = line.replace('\n', '').replace('\r', '')

            if not len(line):
                continue

            # Strip the output part
            line = line.split("#")[0]

            entries = line.split(',')

            # There need to be exactly 10 entries for every fragmentation
            if len(entries) != 10:
                print("ERROR! Cannot read fragmentation line:")
                print(line)
                continue

            # Create a new fragmentation entry
            frag_entry = FragmentationEntry(*entries, normalize_units=True)

            self.fragmentation_entries.append(frag_entry)


        # Set the fragmentation entries to constants
        self.gui.const.fragmentation_entries = self.fragmentation_entries


    def loadFragmentationFile(self):
        """ Load fragmentation paramters from the fragmentation file. """

        string = ""
        with open(self.fragmentation_file_path) as f:
            for line in f:
                string += line


        self.loadFromString(string)


    def toString(self):
        """ Convert the container to a string. """

        # Write the header
        out_str  = ""
        out_str += """# MetSim fragmentation file.
#
# Types of entries:
#   - *INIT - Initial parameters taken from the GUI. Ready only, cannot be set in this file.
#   - M    - Main fragment - parameter change.
#           - REQUIRED: Height.
#           - Possible: Ablation coeff, Erosion coeff, Grain masses, Mass index.
#   - A    - All fragments - parameter change.
#           - REQUIRED: Height.
#           - Possible: Ablation coeff, Gamma.
#   - F    - New single-body fragment.
#           - REQUIRED: Height, Number, Mass (%).
#           - Possible: Ablation coeff.
#   - EF   - New eroding fragment. A mass index of 2.0 will be assumed if not given.
#           - REQUIRED: Height, Number, Mass (%), Erosion coeff, Grain masses.
#           - Possible: Ablation coeff, Mass index.
#   - D    - Dust release. Only the grain mass range needs to be specified. A mass index of 2.0 will be assumed if not given.
#           - REQUIRED: Height, Mass (%), Grain MIN mass, Grain MAX mass.
#           - Possible: Mass index.
#
#                             INPUTS (leave unchanged fields empty)                                      #        OUTPUTS  (do not fill in!)                                  #
# ------------------------------------------------------------------------------------------------------ # ------------------------------------------------------------------ #
# Type, Height (km), Number, Mass (%), Ablation coeff, Gamma, Erosion coeff, Grain MIN, Grain MAX, Mass  #  Time (s),  Dyn pres, Velocity, Parent mass, Mass (kg), Final mass #
#     ,            ,       ,         , (s^2 km^-2)   ,      , (s^2 km^-2)  , mass (kg), mass (kg), index #          ,  (kPa)   , (km/s)  , (kg)       ,          , (kg)       #
#-----,------------,-------,---------,---------------,------,--------------,----------,----------,-------#----------,----------,---------,------------,----------,----------- #
"""
    
    
        frag_entry_list = []

        # Write the initial parameters
        initial_entry = FragmentationEntry(frag_type="# INIT",
                                            height=self.gui.const.h_init,
                                            number=1,
                                            mass_percent=100,
                                            sigma=self.gui.const.sigma,
                                            gamma=self.gui.const.gamma,
                                            erosion_coeff=0.0,
                                            grain_mass_min=None,
                                            grain_mass_max=None,
                                            mass_index=None
                                            )

        frag_entry_list.append(initial_entry)
        frag_entry_list += self.fragmentation_entries

        # Write the last parameters of the main fragment
        if self.gui.simulation_results is not None:

            final_fragment = FragmentationEntry(frag_type="# END",
                                                height=self.gui.simulation_results.frag_main.h,
                                                number=1,
                                                mass_percent=None,
                                                sigma=None,
                                                gamma=None,
                                                erosion_coeff=None,
                                                grain_mass_min=None,
                                                grain_mass_max=None,
                                                mass_index=None
                )

            final_fragment.final_mass = self.gui.simulation_results.frag_main.m

            frag_entry_list.append(final_fragment)


        # Write fragmentation entries
        for frag_entry in frag_entry_list:
            out_str += frag_entry.toString() + "\n"


        return out_str



    def writeFragmentationFile(self):

        with open(self.fragmentation_file_path, 'w') as f:
            f.write(self.toString())

        print("Fragmentation file saved:", self.fragmentation_file_path)


    def newFragmentationFile(self):
        """ Open a new fragmentation file. """

        # Reset fragmentation entries
        self.fragmentation_entries = []

        # Write an empty fragmentation file to disk
        self.writeFragmentationFile()

        self.gui.const.fragmentation_file_name = os.path.basename(self.fragmentation_file_path)


    def addFragmentation(self, frag_type):
        """ Add a new fragmentation entry. """

        # Load the fragmentation file
        self.loadFragmentationFile()


        # Change paramters of the main fragment
        if frag_type == "M":
            frag_entry = FragmentationEntry(frag_type=frag_type,
                                            height=100000.0000,
                                            number=None,
                                            mass_percent=None,
                                            sigma=None,
                                            gamma=None,
                                            erosion_coeff=self.gui.const.erosion_coeff,
                                            grain_mass_min=None,
                                            grain_mass_max=None,
                                            mass_index=None
                                            )

        # Change paramters of all fragments
        elif frag_type == "A":
            frag_entry = FragmentationEntry(frag_type=frag_type,
                                            height=100000.0,
                                            number=None,
                                            mass_percent=None,
                                            sigma=self.gui.const.sigma,
                                            gamma=self.gui.const.gamma,
                                            erosion_coeff=None,
                                            grain_mass_min=None,
                                            grain_mass_max=None,
                                            mass_index=None
                                            )

        # New single-body fragment
        elif frag_type == "F":
            frag_entry = FragmentationEntry(frag_type=frag_type,
                                            height=100000.0,
                                            number=1,
                                            mass_percent=10,
                                            sigma=self.gui.const.sigma,
                                            gamma=None,
                                            erosion_coeff=None,
                                            grain_mass_min=None,
                                            grain_mass_max=None,
                                            mass_index=None
                                            )

        elif frag_type == "EF":
            frag_entry = FragmentationEntry(frag_type=frag_type,
                                            height=100000.0,
                                            number=1,
                                            mass_percent=10,
                                            sigma=None,
                                            gamma=None,
                                            erosion_coeff=self.gui.const.erosion_coeff,
                                            grain_mass_min=self.gui.const.erosion_mass_min,
                                            grain_mass_max=self.gui.const.erosion_mass_max,
                                            mass_index=self.gui.const.erosion_mass_index
                                            )

        elif frag_type == "D":
            frag_entry = FragmentationEntry(frag_type=frag_type,
                                            height=100000.0,
                                            number=None,
                                            mass_percent=10,
                                            sigma=None,
                                            gamma=None,
                                            erosion_coeff=None,
                                            grain_mass_min=self.gui.const.erosion_mass_min,
                                            grain_mass_max=self.gui.const.erosion_mass_max,
                                            mass_index=self.gui.const.erosion_mass_index
                                            )

        else:
            print("ERROR! Unknown fragmentation type:", frag_type)


        # Add the fragment entry
        self.fragmentation_entries.append(frag_entry)
        self.gui.const.fragmentation_entries = self.fragmentation_entries


        # Save the fragmentation file
        self.writeFragmentationFile()






class MinimizationParameterNormalization(object):
    def __init__(self, params):
        """ Normalize the fit parameters so that they are approx in the 0 to 1 range.

            The normalization can be done by giving the fit bounds, in which case all parameters will be
            normalized to the 0 to 1 range. Or, a fixed value can be given for every parameter which should
            bring it approx to the 0 to 1 range.

        Arguments:
            params: [list] A list of fit parameters.


        """

        self.params = params

        self.normalization_method = None
        self.bounds = None
        self.scaling_list = None



    def normalizeBounds(self, bounds):
        """ Normalize the parameters given the fit bounds. 
        
        Arguments:
            bounds: [list] A list of bounds for every parameter.

        Return:
            params_normed: [list] A list of normalized parameters.
            bounds_normed: [list] A list of normalized boundaries to the [0, 1] range.
        """

        self.normalization_method = 'bounds'
        self.bounds = bounds

        # Normalize every parameter
        params_normed = []
        bounds_normed = []
        for p, bound in zip(self.params, self.bounds):

            bound_min, bound_max = bound

            # Normalize the parameter to [0, 1] range
            p_normed = (p - bound_min)/(bound_max - bound_min)

            params_normed.append(p_normed)

            bounds_normed.append([0, 1])


        return params_normed, bounds_normed



    def normalizeScaling(self, scaling_list, bounds=None):
        """ Normalize the parameters by specifying a scale for every paramter. 
    
        Arguments:
            scaling_list: [list] A list of values used for scaling each parameter to approx the 0 to 1 range
                using multiplication.

        Keyword arguments:
            bounds: [list] A list of bounds which will be scaled. None by default.


        Return;
            if bounds is None:
                params_normed: [list] A list of scaled paramters.
            else:
                params_normed: [list] A list of scaled paramters.
                bounds_normed: [list] A list of scaled boundaries.
        """

        self.normalization_method = 'scaling'
        self.scaling_list = scaling_list
        self.bounds = bounds

        params_normed = []
        bounds_normed = []

        for i, (p, scale) in enumerate(zip(self.params, self.scaling_list)):

            # Scale the parameter
            p_normed = p*scale

            params_normed.append(p_normed)

            # Scale the bounds, if given
            if bounds is not None:
                bound = bounds[i]
                bound_normed = [b*scale for b in bound]
                bounds_normed.append(bound_normed)

        
        if bounds is None:
            return params_normed

        else:
            return params_normed, bounds_normed



    def denormalize(self, params_normed):
        """ Denormalize the given normalized parameters. 
        
        Arguments:
            params_normed: [list] A list of normalized parameters.

        Return:
            params: [list] A list of denormalized parameters.
        """

        # Denormalize using bounds
        if self.normalization_method == 'bounds':

            if self.bounds is None:
                raise ValueError("Boundaries for parameter denormalization not specified!")


            params = []

            for p_normed, bound in zip(params_normed, self.bounds):

                bound_min, bound_max = bound

                # Denormalize the paramer to original bounds
                p = p_normed*(bound_max - bound_min) + bound_min

                params.append(p)


        # Denormalize using scaling
        elif self.normalization_method == 'scaling':

            if self.scaling_list is None:
                raise ValueError("Scaling list for parameter denormalization not specified!")

            params = []

            for p_normed, scale in zip(params_normed, self.scaling_list):

                p = p_normed/scale

                params.append(p)


        return params




def extractConstantParams(const_original, params, param_string, mini_norm_handle):
    """ Assign parameters to a Constants object given an array of parameters. """

    # Create a copy of fit parameters
    const = copy.deepcopy(const_original)


    ### Extract fit parameters ###


    # Denormalize the fit parameters from the [0, 1] range
    params = mini_norm_handle.denormalize(params)

    # Read meteoroid properties
    param_index = 4
    const.m_init, const.v_init, const.rho, const.sigma = params[:param_index]

    # Read erosion parameters if the erosion is on
    if 'e' in param_string:
        const.erosion_height_start, const.erosion_coeff, const.erosion_height_change, \
            const.erosion_coeff_change, const.erosion_mass_index, const.erosion_mass_min, \
            const.erosion_mass_max = params[param_index:param_index + 7]

        param_index += 7


    # Read distuption parameters if the disruption is on
    if 'd' in param_string:
        const.compressive_strength, const.disruption_erosion_coeff, const.disruption_mass_index, \
            const.disruption_mass_min_ratio, const.disruption_mass_max_ratio, \
            const.disruption_mass_grain_ratio = params[param_index:param_index + 6]

        param_index += 6

    ### ###
    
    return const




def fitResiduals(params, fit_input_data, param_string, const_original, traj, mini_norm_handle, 
    mag_weight=10.0, lag_weights=None, lag_weight_ht_change=0.0, verbose=True, gui_handle=None):
    """ Compute the fit residual. """

    if verbose:
        print()
        print('Params:')
        # print(params)
        print(mini_norm_handle.denormalize(params))


    if lag_weights is None:
        lag_weights = [1.0, 1.0]


    # Assign fit parameters to a constants object
    const = extractConstantParams(const_original, params, param_string, mini_norm_handle)


    # Run the simulation
    frag_main, results_list, wake_results = runSimulation(const, compute_wake=False)

    # Store simulation results
    sr = SimulationResults(const, frag_main, results_list, wake_results)


    ### Compute the residuals ###

    # Count the total number of length and magnitude points
    mag_point_count = len(~np.isnan(fit_input_data[:, 1]))
    lag_point_count = len(~np.isnan(fit_input_data[:, 2]))

    # Interpolate simulated magnitude and length
    len_sim_interpol = scipy.interpolate.interp1d(sr.brightest_height_arr, sr.brightest_length_arr, \
        bounds_error=False, fill_value=0)
    mag_sim_interpol = scipy.interpolate.interp1d(sr.brightest_height_arr, sr.abs_magnitude, \
        bounds_error=False, fill_value=10)
    time_sim_interpol = scipy.interpolate.interp1d(sr.brightest_height_arr, sr.time_arr, \
        bounds_error=False, fill_value=0)

    
    # Find the length and time at the meteor begin point
    begin_length_sim = len_sim_interpol(traj.rbeg_ele)
    begin_time_sim = time_sim_interpol(traj.rbeg_ele)


    # Go through every height point and compute the residual contribution
    total_mag_residual = 0
    total_lag_residual = 0
    for entry in fit_input_data:

        height, mag, lag = entry


        # Find the corresponding magnitude and length from the simulation to the observation
        mag_sim = mag_sim_interpol(height)
        lag_sim = len_sim_interpol(height) - begin_length_sim \
            - traj.orbit.v_init*(time_sim_interpol(height) - begin_time_sim)


        # print("{:.2f}, {:.2f}, {:.2f}, {:.0f}, {:.0f}".format(height, mag, mag_sim, lag, lag_sim))


        # # Compute the magnitude residual 
        # mag_res = 0
        # if not np.isnan(mag):
        #     mag_res = abs((mag - mag_sim)*mag_weight)

        # if np.isnan(mag_res):
        #     mag_res = 10.0

        # Compute the magnitude residual in linear luminosity units
        if not np.isnan(mag):
            lum_obs = const.P_0m*10**(-0.4*mag)
            lum_sim = const.P_0m*10**(-0.4*mag_sim)

            mag_res = mag_weight*(lum_obs - lum_sim)**2

        if np.isnan(mag_res):
            mag_res = 1e6**2


        # Choose the lag weight
        if height > lag_weight_ht_change:
            lag_weight = lag_weights[0]
        else:
            lag_weight = lag_weights[1]


        # Compute the length residual
        lag_res = 0
        if not np.isnan(lag):
            lag_res = lag_weight*(lag - lag_sim)**2

        if np.isnan(lag_res):
            lag_res = 10000**2


        total_mag_residual += mag_res
        total_lag_residual += lag_res

    total_residual = total_mag_residual/mag_point_count + total_lag_residual/lag_point_count


    # If the total residual is zero, make it very high
    if total_residual == 0:
        total_residual = 1e8


    if verbose:
        print('Total residual:', total_residual)

        if gui_handle is not None:

            # Plot the results of the current fit
            gui_handle.simulation_results = sr
            gui_handle.const = const
            gui_handle.showCurrentResults()
            gui_handle.repaint()


    return total_residual



# Modify the residuals function so that it takes a list of arguments
def fitResidualsListArguments(params, *args, **kwargs):
    return [fitResiduals(param_line, *args, **kwargs) for param_line in params]



def loadConstants(sim_fit_json):
    """ Load the simulation constants from a JSON file. 
        
    Arguments:
        sim_fit_json: [str] Path to the sim_fit JSON file.

    Return:
        (const, const_json): 
            - const: [Constants object]
            - const_json: [dict]

    """

    # Init the constants
    const = Constants()


    # Load the nominal simulation
    with open(sim_fit_json) as f:
        const_json = json.load(f)


        # Fill in the constants
        for key in const_json:
            setattr(const, key, const_json[key])


    if 'fragmentation_entries' in const_json:

        # Convert fragmentation entries from dictionaties to objects
        frag_entries = []
        if len(const_json['fragmentation_entries']) > 0:
            for frag_entry_dict in const_json['fragmentation_entries']:

                # Only take entries which are variable names for the FragmentationEntry class
                frag_entry_dict = {key:frag_entry_dict[key] for key in frag_entry_dict \
                    if key in FragmentationEntry.__init__.__code__.co_varnames}

                frag_entry = FragmentationEntry(**frag_entry_dict)
                frag_entries.append(frag_entry)

        const.fragmentation_entries = frag_entries


    return const, const_json



class MetSimGUI(QMainWindow):
    def __init__(self, traj_path, const_json_file=None, met_path=None, lc_path=None, wid_files=None):
        """ GUI tool for MetSim. 
    
        Arguments:
            traj_path: [str] Path to the trajectory pickle file.

        Keyword arguments:
            const_json_file: [str] Path to the JSON file with simulation parameters.
            met_path: [str] Path to the METAL or mirfit .met file with additional magnitude or lag information.
            lc_path: [str] Path to the light curve CSV file.
            wid_files: [str] Mirfit wid files containing the meteor wake information.
        """
        

        self.traj_path = traj_path

        # Load the trajectory pickle file
        self.traj = loadPickle(*os.path.split(traj_path))

        # Extract the directory path
        self.dir_path = os.path.dirname(traj_path)


        ### LOAD .met FILE ###

        # Load a METAL .met file if given
        self.met = None
        if met_path is not None:
            if os.path.isfile(met_path):
                self.met = loadMet(*os.path.split(os.path.abspath(met_path)))
            else:
                print('The .met file does not exist:', met_path)
                sys.exit()

        # Init a container for .met results, compute lag and magnitude
        if self.met is not None:
            self.met_obs = MetObservations(self.met, self.traj)
        else:
            self.met_obs = None

        ### ###



        ### LOAD the light curve file ###
        self.lc_data = None
        if lc_path is not None:
            if os.path.isfile(lc_path):

                # Read the light curve data
                self.lc_data = LightCurveContainer(*os.path.split(os.path.abspath(lc_path)))

            else:
                print("The light curve file does not exist:", lc_path)
                sys.exit()


        ### ###



        ### LOAD WAKE FILES ###

        # Load mirfit wake "wid" file
        self.wake_meas = []
        if wid_files is not None:
            for wid_path in wid_files:
                for wid_file in glob.glob(wid_path):

                    # Load wake information from wid files
                    wake_container = self.loadWakeFile(wid_file)

                    if wake_container is not None:
                        self.wake_meas.append(wake_container)

        
        # Get a list of heights where lag measurements were taken
        self.wake_heights = None
        if self.wake_meas:
            self.wake_heights = []
            for wake_container in self.wake_meas:
                for wake_pt in wake_container.points:
                    if int(wake_pt.n ) == 0:
                        self.wake_heights.append([wake_pt.ht, wake_container])
                        break

            # Sort wake height list by height
            self.wake_heights = sorted(self.wake_heights, key=lambda x: x[0])

        ### ###



        ### Init GUI ###

        QMainWindow.__init__(self)

        # Load the GUI design file
        loadUi(os.path.join(os.path.dirname(__file__), "GUI.ui"), self)

        self.setWindowTitle("MetSim")

        ### ###


        ### ### Define GUI and simulation attributes ### ###


        # Init an axis for the electron line density
        self.electronDensityPlot = self.magnitudePlot.canvas.axes.twiny()
        self.electron_density_plot_show = False


        ### Wake parameters ###
        self.wake_on = False
        self.wake_show_mass_bins = False
        self.wake_ht_current_index = 0
        self.current_wake_container = None

        if self.wake_heights is not None:
            self.wake_plot_ht, self.current_wake_container = self.wake_heights[self.wake_ht_current_index]
        else:
            self.wake_plot_ht = self.traj.rbeg_ele # m

        self.wake_normalization_method = 'area'
        self.wake_align_method = 'none'


        self.magnitudePlotWakeLines = None
        self.magnitudePlotWakeLineLabels = None
        self.velocityPlotWakeLines = None
        self.lagPlotWakeLines = None

        ### ###


        ### Autofit parameters ###


        self.autofit_method = "Local"
        self.autoFitMethodToggle(self.autofit_method)

        self.pso_iterations = 10
        self.pso_particles = 100

        self.autofit_mag_weight = 0.1
        self.autofit_lag_weight_ht_change = self.traj.rend_ele - 10000
        self.autofit_lag_weights = [1.0, 1.0]

        ### ###
    

        # Disable different density after erosion change
        self.erosion_different_rho = False

        # Disable different ablation coeff after erosion change
        self.erosion_different_sigma = False

        # Disable different erosion coeff after disruption at the beginning
        self.disruption_different_erosion_coeff = False


        # Fragmentation object
        self.fragmentation = None


        self.simulation_results = None

        self.const_prev = None
        self.simulation_results_prev = None


        ### ### ### ###


        ### Init simulation parameters ###

        # Init the constants
        self.const = Constants()

        # If a JSON file with constant was given, load them instead of initing from scratch
        if const_json_file is not None:

            # Load the constants from the JSON files
            self.const, const_json = loadConstants(const_json_file)


            # Init the fragmentation container for the GUI
            if len(self.const.fragmentation_entries):
            
                self.fragmentation = FragmentationContainer(self, \
                    os.path.join(self.dir_path, self.const.fragmentation_file_name))
                self.fragmentation.fragmentation_entries = self.const.fragmentation_entries

                # Overwrite the existing fragmentatinon file
                self.fragmentation.writeFragmentationFile()




            # Check if the disruption erosion coefficient is different than the main erosion coeff
            if const_json['disruption_erosion_coeff'] != const_json['erosion_coeff']:
                self.disruption_different_erosion_coeff = True


            # Check if the density is changed after Hchange
            if 'erosion_rho_change' in const_json:
                if const_json['erosion_rho_change'] != const_json['rho']:
                    self.erosion_different_rho = True

            # Check if the ablation coeff is changed after Hchange
            if 'erosion_sigma_change' in const_json:
                if const_json['erosion_sigma_change'] != const_json['sigma']:
                    self.erosion_different_sigma = True

            # if 'fragmentation_string' in const_json:
            #     if const_json['fragmentation_string'] is not None:
            #         self.fragmentation = FragmentationContainer(self, os.path.join(self.dir_path, \
            #             "metsim_fragmentation.txt"))
            #         self.fragmentation.loadFromString(const_json['fragmentation_string'])
            #         self.fragmentation.writeFragmentationFile()


            # # Convert the density coefficients into a numpy array
            # self.const.dens_co = np.array(self.const.dens_co)


        else:

            # Set the constants value from the trajectory
            self.const.zenith_angle = self.traj.orbit.zc
            self.const.v_init = self.traj.orbit.v_init

            # Set kill height to the observed end height
            self.const.h_kill = self.traj.rend_ele - 3000

            # Set erosion heights to the beginning/end height
            self.const.erosion_height_start = self.traj.rbeg_ele
            self.const.erosion_height_change = self.traj.rend_ele

            # Disable erosion and disruption at the beginning
            self.const.erosion_on = False
            self.const.disruption_on = False


            # Calculate the photometric mass
            self.const.m_init = self.calcPhotometricMass()
            print("Using initial mass: {:.2e} kg".format(self.const.m_init))


        ### ###



        ### Calculate atmosphere density coeffs (down to the bottom observed height, limit to 15 km) ###

        # Determine the height range for fitting the density
        self.dens_fit_ht_beg = self.const.h_init
        self.dens_fit_ht_end = self.traj.rend_ele - 5000
        if self.dens_fit_ht_end < 15000:
            self.dens_fit_ht_end = 15000

        # Fit the polynomail describing the density
        dens_co = self.fitAtmosphereDensity(self.dens_fit_ht_beg, self.dens_fit_ht_end)
        self.const.dens_co = dens_co

        print("Atmospheric mass density fit for the range of heights: {:.2f} - {:.2f} km".format(\
            self.dens_fit_ht_end/1000, self.dens_fit_ht_beg/1000))

        ### ###



        # Update the values in the input boxes
        self.updateInputBoxes()



        ### Add key bindings ###

        # Update shown grain diameters when the grain mass is updated
        self.inputErosionMassMin.editingFinished.connect(self.updateGrainDiameters)
        self.inputErosionMassMax.editingFinished.connect(self.updateGrainDiameters)

        # Update the photometric mass when the luminous efficiency is changed
        self.inputLumEff.editingFinished.connect(self.updateInitialMass)

        # Update boxes when the luminous efficiency type is changes
        self.lumEffComboBox.currentIndexChanged.connect(self.updateLumEffType)

        self.wakePlotUpdateButton.clicked.connect(self.updateWakePlot)
        self.wakeIncrementPlotHeightButton.clicked.connect(self.incrementWakePlotHeight)
        self.wakeDecrementPlotHeightButton.clicked.connect(self.decrementWakePlotHeight)
        self.wakeSaveVideoButton.clicked.connect(self.saveVideo)

        self.radioButtonWakeNormalizationPeak.toggled.connect(self.toggleWakeNormalizationMethod)
        self.radioButtonWakeNormalizationArea.toggled.connect(self.toggleWakeNormalizationMethod)
        self.radioButtonWakeNormalizationArea.setChecked(self.wake_normalization_method == 'area')
        self.radioButtonWakeNormalizationPeak.setChecked(self.wake_normalization_method == 'peak')
        
        self.radioButtonWakeAlignNone.toggled.connect(self.toggleWakeAlignMethod)
        self.radioButtonWakeAlignPeak.toggled.connect(self.toggleWakeAlignMethod)
        self.radioButtonWakeAlignCorrelate.toggled.connect(self.toggleWakeAlignMethod)
        self.radioButtonWakeAlignNone.setChecked(self.wake_align_method == 'none')
        self.radioButtonWakeAlignPeak.setChecked(self.wake_align_method == 'peak')
        self.radioButtonWakeAlignCorrelate.setChecked(self.wake_align_method == 'correlate')

        #self.addToolBar(NavigationToolbar(self.magnitudePlot.canvas, self))


        self.autoFitMethodComboBox.activated[str].connect(self.autoFitMethodToggle)


        self.checkBoxWake.stateChanged.connect(self.checkBoxWakeSignal)
        self.checkBoxWakeMassBins.stateChanged.connect(self.checkBoxWakeMassBinsSignal)
        self.checkBoxErosion.stateChanged.connect(self.checkBoxErosionSignal)
        self.checkBoxErosionRhoChange.stateChanged.connect(self.checkBoxErosionRhoSignal)
        self.checkBoxErosionAblationCoeffChange.stateChanged.connect(self.checkBoxErosionAblationCoeffSignal)
        self.checkBoxDisruption.stateChanged.connect(self.checkBoxDisruptionSignal)
        self.checkBoxDisruptionErosionCoeff.stateChanged.connect(self.checkBoxDisruptionErosionCoeffSignal)


        self.runSimButton.clicked.connect(self.runSimulationGUI)
        self.autoFitButton.clicked.connect(self.autoFit)


        self.fragmentationGroup.toggled.connect(self.toggleFragmentation)
        self.newFragmentationFileButton.clicked.connect(self.newFragmentationFile)
        self.checkBoxFragmentationShowIndividualLCs.clicked.connect(self.checkBoxFragmentationShowIndividualLCsSignal)
        self.loadFragmentationFileButton.clicked.connect(self.loadFragmentationFile)
        self.mainFragmentStatusChangeButton.clicked.connect(lambda x: self.addFragmentation("M"))
        self.allFragmentsStatusChangeButton.clicked.connect(lambda x: self.addFragmentation("A"))
        self.newSingleBodyFragmentButton.clicked.connect(lambda x: self.addFragmentation("F"))
        self.newErodingFragmentButton.clicked.connect(lambda x: self.addFragmentation("EF"))
        self.newDustReleaseButton.clicked.connect(lambda x: self.addFragmentation("D"))

        
        self.showPreviousButton.pressed.connect(self.showPreviousResults)
        self.showPreviousButton.released.connect(self.showCurrentResults)

        self.saveUpdatedOrbitButton.clicked.connect(self.saveUpdatedOrbit)
        self.saveFitParametersButton.clicked.connect(self.saveFitParameters)

        # Electron line density
        self.checkBoxPlotElectronLineDensity.toggled.connect(self.checkBoxPlotElectronLineDensitySignal)
        self.inputElectronDensityHt.editingFinished.connect(self.electronDensityMeasChanged)
        self.inputElectronDensity.editingFinished.connect(self.electronDensityMeasChanged)

        # Plots
        self.plotAirDensityButton.clicked.connect(self.plotAtmDensity)
        self.plotDynamicPressureButton.clicked.connect(self.plotDynamicPressure)
        self.plotMassLossButton.clicked.connect(self.plotMassLoss)


        ### ###

        # Update checkboxes
        self.checkBoxWakeSignal(None)
        self.checkBoxErosionSignal(None)
        self.checkBoxErosionRhoSignal(None)
        self.checkBoxErosionAblationCoeffSignal(None)
        self.checkBoxDisruptionSignal(None)
        self.checkBoxDisruptionErosionCoeffSignal(None)
        self.checkBoxDisruptionErosionCoeffSignal(None)
        self.toggleWakeNormalizationMethod(None)
        self.toggleWakeAlignMethod(None)
        self.toggleFragmentation(None)


        # Compute plot height limits
        self.plot_beg_ht, self.plot_end_ht = self.calcHeightLimits()

        # Update plots
        self.showCurrentResults()



    def fitAtmosphereDensity(self, dens_fit_ht_beg, dens_fit_ht_end):
        """ Fit the atmosphere density coefficients for the given day and location. 
        
        Arguments:
            dens_fit_ht_beg: [float] Begin height (top) for which the fit is valid (meters).
            dens_fit_ht_end: [float] End height - bottom (meters).

        """

        # Take mean meteor lat/lon as reference for the atmosphere model
        lat_mean = np.mean([self.traj.rbeg_lat, self.traj.rend_lat])
        lon_mean = meanAngle([self.traj.rbeg_lon, self.traj.rend_lon])

        return fitAtmPoly(lat_mean, lon_mean, dens_fit_ht_end, dens_fit_ht_beg, self.traj.jdt_ref)


    def loadWakeFile(self, file_path):
        """ Load a mirfit wake "wid" file. """


        # Extract the site ID and the frame number from the file name
        site_id, frame_n = os.path.basename(file_path).replace('.txt', '').split('_')[1:]
        site_id = str(int(site_id))
        frame_n = int(frame_n)

        print('wid file: ', site_id, frame_n, end='')


        # Extract geo coordinates of sites
        lat_dict = {obs.station_id:obs.lat for obs in self.traj.observations}
        lon_dict = {obs.station_id:obs.lon for obs in self.traj.observations}
        ele_dict = {obs.station_id:obs.ele for obs in self.traj.observations}

        wake_container = None
        leading_state_vect_dist = 0
        with open(file_path) as f:
            for line in f:

                if line.startswith('#'):
                    continue

                line = line.replace('\n', '').replace('\r', '').split()

                if not line:
                    continue


                # Init the wake container
                if wake_container is None:
                    wake_container = WakeContainter(site_id, frame_n)

                # Read the wake point
                n, th, phi, _, _, _, _, intens_sum, amp, r, b, c = list(map(float, line))

                # Skip bad measurements
                if np.any(np.isnan([th, phi])):
                    continue

                ### Compute the projection of the wake line of sight to the trajectory ###

                # Calculate RA/Dec
                ra, dec = altAz2RADec(np.pi/2 - np.radians(phi), np.pi/2 - np.radians(th), \
                    self.traj.jdt_ref, lat_dict[site_id], lon_dict[site_id])

                # Compute the station coordinates at the given time
                stat = geo2Cartesian(lat_dict[site_id], lon_dict[site_id], ele_dict[site_id], \
                    self.traj.jdt_ref)

                # Compute measurement rays in cartesian coordinates
                meas = np.array(raDec2ECI(ra, dec))

                # Calculate closest points of approach (observed line of sight to radiant line)
                obs_cpa, rad_cpa, d = findClosestPoints(stat, meas, self.traj.state_vect_mini, \
                    self.traj.radiant_eci_mini)

                # If the projected point is above the state vector, use negative lengths
                state_vect_dist_sign = 1.0
                if vectMag(rad_cpa) > vectMag(self.traj.state_vect_mini):
                    state_vect_dist_sign = -1.0


                # Compute Distance from the state vector to the projected point on the radiant line
                state_vect_dist = state_vect_dist_sign*vectMag(self.traj.state_vect_mini - rad_cpa)

                # Compute the height (meters)
                _, _, ht = cartesian2Geo(self.traj.jdt_ref, *rad_cpa)

                ### ###

                # Record the state vector distance of the leading fragment
                if int(n) == 0:
                    leading_state_vect_dist = state_vect_dist

                wake_container.addPoint(n, th, phi, intens_sum, amp, r, b, c, state_vect_dist, ht)


            # If there are no points in the wake container, don't use it
            if wake_container is not None:
                if len(wake_container.points) == 0:
                    wake_container = None

            # Compute lengths of the leading fragment
            if wake_container is not None:
                for wake_pt in wake_container.points:
                    wake_pt.leading_frag_length = wake_pt.state_vect_dist - leading_state_vect_dist

        if wake_container is None:
            print("... rejected")
        else:
            print("... loaded!")

        return wake_container



    def calcPhotometricMass(self):
        """ Calculate photometric mass from given magnitude data. """

        print()

        # If the magnitudes are given from the met file, use them instead of the trajectory file
        time_mag_arr = []
        avg_t_diff_max = 0
        if self.met_obs is not None:

            print("Photometric mass computed from the .met file!")

            # Extract time vs. magnitudes from the met file
            for site in self.met_obs.sites:

                # Extract data
                abs_mag_data = self.met_obs.abs_mag_data[site]
                time_data = self.met_obs.time_data[site]

                # Compute the average time difference
                avg_t_diff_max = max(avg_t_diff_max, np.median(time_data[1:] - time_data[:-1]))

                for t, mag in zip(time_data, abs_mag_data):
                    if (mag is not None) and (not np.isnan(mag)):
                        time_mag_arr.append([t, mag])

        # If the magnitudes are given
        elif self.lc_data is not None:

            print("Photometric mass computed from the light curve file!")

            # Plot additional magnitudes for all sites
            for site in self.lc_data.sites:

                # Extract data
                abs_mag_data = self.lc_data.abs_mag_data[site]
                time_data = self.lc_data.time_data[site]

                # Compute the average time difference
                avg_t_diff_max_tmp = max(avg_t_diff_max, np.median(time_data[1:] - time_data[:-1]))
                if avg_t_diff_max == 0:
                    avg_t_diff_max = avg_t_diff_max_tmp
                else:
                    avg_t_diff_max = max([avg_t_diff_max, avg_t_diff_max_tmp])

                for t, mag in zip(time_data, abs_mag_data):
                    if (mag is not None) and (not np.isnan(mag)):
                        time_mag_arr.append([t, mag])

        else:

            print("Photometric mass computed from the trajectory pickle file!")

            # Extract time vs. magnitudes from the trajectory pickle file
            for obs in self.traj.observations:

                # If there are not magnitudes for this site, skip it
                if obs.absolute_magnitudes is None:
                    continue

                # Compute average time difference
                avg_t_diff_max = max(avg_t_diff_max, np.median(obs.time_data[1:] - obs.time_data[:-1]))

                for t, mag in zip(obs.time_data[obs.ignore_list == 0], \
                    obs.absolute_magnitudes[obs.ignore_list == 0]):

                    if (mag is not None) and (not np.isnan(mag)):
                        time_mag_arr.append([t, mag])

        
        # If there are no magnitudes, assume that the initial mass is 0.2 grams
        if not time_mag_arr:
            print("No photometry, assuming default mass:", self.const.m_init)
            return self.const.m_init

        print("NOTE: The mass was computing using a constant luminous efficiency defined in the GUI!")

        # Sort array by time
        time_mag_arr = np.array(sorted(time_mag_arr, key=lambda x: x[0]))

        time_arr, mag_arr = time_mag_arr.T

        
        # Average out the magnitudes
        time_arr, mag_arr = mergeClosePoints(time_arr, mag_arr, avg_t_diff_max, method='avg')

        # Compute the photometry mass
        return calcMass(np.array(time_arr), np.array(mag_arr), self.traj.orbit.v_avg, \
            tau=self.const.lum_eff/100, P_0m=self.const.P_0m)



    def updateInputBoxes(self, show_previous=False):
        """ Update input boxes with values from the Constants object. """

        # Choose to show current or previous simulation parameters
        if show_previous and (self.const_prev is not None):
            const = self.const_prev
        else:
            const = self.const

        
        ### Simulation params ###

        self.inputTimeStep.setText(str(const.dt))
        self.inputHtInit.setText("{:.3f}".format(const.h_init/1000))
        self.inputP0M.setText("{:d}".format(int(const.P_0m)))
        self.inputMassKill.setText("{:.1e}".format(const.m_kill))
        self.inputVelKill.setText("{:.3f}".format(const.v_kill/1000))
        self.inputHtKill.setText("{:.3f}".format(const.h_kill/1000))

        ### ###


        ### Meteoroid physical properties ###

        self.inputRho.setText("{:d}".format(int(const.rho)))
        self.inputRhoGrain.setText("{:d}".format(int(const.rho_grain)))
        self.inputMassInit.setText("{:.1e}".format(const.m_init))
        self.inputAblationCoeff.setText("{:.3f}".format(const.sigma*1e6))
        self.inputVelInit.setText("{:.3f}".format(const.v_init/1000))
        self.inputShapeFact.setText("{:.2f}".format(const.shape_factor))
        self.inputGamma.setText("{:.2f}".format(const.gamma))
        self.inputZenithAngle.setText("{:.3f}".format(np.degrees(const.zenith_angle)))
        self.inputLumEff.setText("{:.2f}".format(const.lum_eff))
        
        self.lumEffComboBox.setCurrentIndex(const.lum_eff_type)

        # Enable/disable the constant tau box (index 0 = constant tau)
        self.inputLumEff.setDisabled(self.const.lum_eff_type != 0)

        ### ###


        ### Wake parameters ###

        self.checkBoxWake.setChecked(self.wake_on)
        self.checkBoxWakeMassBins.setChecked(self.wake_show_mass_bins)

        self.inputWakePSF.setText("{:.1f}".format(const.wake_psf))
        self.inputWakeExt.setText("{:d}".format(int(const.wake_extension)))
        self.inputWakePlotHt.setText("{:.3f}".format(self.wake_plot_ht/1000))

        ### ###


        ### Erosion parameters ###

        self.checkBoxErosion.setChecked(const.erosion_on)
        self.checkBoxErosionRhoChange.setChecked(self.erosion_different_rho)
        self.checkBoxErosionAblationCoeffChange.setChecked(self.erosion_different_sigma)
        self.inputErosionHtStart.setText("{:.3f}".format(const.erosion_height_start/1000))
        self.inputBinsPer10m.setText("{:d}".format(const.erosion_bins_per_10mass))
        self.inputErosionCoeff.setText("{:.3f}".format(const.erosion_coeff*1e6))
        self.inputErosionHtChange.setText("{:.3f}".format(const.erosion_height_change/1000))
        self.inputErosionCoeffChange.setText("{:.3f}".format(const.erosion_coeff_change*1e6))
        self.inputErosionMassIndex.setText("{:.2f}".format(const.erosion_mass_index))
        self.inputErosionMassMin.setText("{:.2e}".format(const.erosion_mass_min))
        self.inputErosionMassMax.setText("{:.2e}".format(const.erosion_mass_max))
        self.inputErosionRhoChange.setText("{:d}".format(int(const.erosion_rho_change)))
        self.inputErosionAblationCoeffChange.setText("{:.3f}".format(const.erosion_sigma_change*1e6))

        ### ###


        ### Disruption parameters ###

        self.checkBoxDisruption.setChecked(const.disruption_on)
        self.checkBoxDisruptionErosionCoeff.setChecked(self.disruption_different_erosion_coeff)
        self.inputDisruptionErosionCoeff.setText("{:.3f}".format(const.disruption_erosion_coeff*1e6))
        self.inputCompressiveStrength.setText("{:.1f}".format(const.compressive_strength/1000))
        self.inputDisruptionMassGrainRatio.setText("{:.2f}".format(const.disruption_mass_grain_ratio*100))
        self.inputDisruptionMassIndex.setText("{:.2f}".format(const.disruption_mass_index))
        self.inputDisruptionMassMinRatio.setText("{:.2f}".format(const.disruption_mass_min_ratio*100))
        self.inputDisruptionMassMaxRatio.setText("{:.2f}".format(const.disruption_mass_max_ratio*100))

        ### ###


        ### Autofit parameters ###

        self.inputAutoFitPSOIterations.setText("{:d}".format(self.pso_iterations))
        self.inputAutoFitPSOParticles.setText("{:d}".format(self.pso_particles))

        self.inputAutoFitMagWeight.setText("{:.1f}".format(self.autofit_mag_weight))
        self.inputAutoFitLagWeightHtChange.setText("{:.2f}".format(self.autofit_lag_weight_ht_change/1000))
        self.inputAutoFitLagWeights.setText("{:.1f}, {:.2f}".format(*self.autofit_lag_weights))

        ### ###


        ### Fragmentation parameters ###
        self.fragmentationGroup.setChecked(const.fragmentation_on)
        self.checkBoxFragmentationShowIndividualLCs.setChecked(const.fragmentation_show_individual_lcs)
        self.checkBoxDisruptionErosionCoeff.setChecked(const.fragmentation_show_individual_lcs)

        ###


        ### Radar measurements ###

        # Height of electron line density measurement
        self.inputElectronDensityHt.setText("{:.3f}".format(self.const.electron_density_meas_ht/1000))

        # Electron line density measurement
        self.inputElectronDensity.setText("{:.2e}".format(self.const.electron_density_meas_q))

        ###


        self.updateGrainDiameters()


    def updateInitialMass(self):
        """ Update the value of the photometric mass. """

        # Keep the old luminous efficiency
        lum_eff_old = self.const.lum_eff

        # Read the new value
        self.const.lum_eff = self._tryReadBox(self.inputLumEff, self.const.lum_eff)

            
        # Rescale the initial mass using the new tau value
        self.const.m_init *= lum_eff_old/self.const.lum_eff


        self.updateInputBoxes()


    def updateLumEffType(self):
        """ Update the luminous efficiency type. """

        self.const.lum_eff_type = self.lumEffComboBox.currentIndex()

        # Enable/disable the constant tau box (index 0 = constant tau)
        self.inputLumEff.setDisabled(self.const.lum_eff_type != 0)


    def updateGrainDiameters(self):
        """ Update the grain diameter labels. """

        # Read minimum and maximum grain masses
        self.const.erosion_mass_min = self._tryReadBox(self.inputErosionMassMin, \
            self.const.erosion_mass_min)
        self.const.erosion_mass_max = self._tryReadBox(self.inputErosionMassMax, \
            self.const.erosion_mass_max)


        # Compute minimum grain diameter (m)
        grain_diam_min = 2*(3/(4.0*np.pi)*self.const.erosion_mass_min/self.const.rho_grain)**(1/3.0)

        # Update diameter label
        self.erosionMinMassDiameter.setText("""d<span style="vertical-align:sub;">min</span> = """ \
            + "{:.0f}".format(1e6*grain_diam_min))


        # Compute maximum grain diameter (m)
        grain_diam_max = 2*(3/(4.0*np.pi)*self.const.erosion_mass_max/self.const.rho_grain)**(1/3.0)

        # Update diameter label
        self.erosionMaxMassDiameter.setText("""d<span style="vertical-align:sub;">max</span> = """ \
            + "{:.0f}".format(1e6*grain_diam_max))




    def checkBoxWakeSignal(self, event):
        """ Control what happens when the wake checkbox is toggled. """

        # Read the wake checkbox
        self.wake_on = self.checkBoxWake.isChecked()

        # Read the mass bins show checkbox
        self.wake_show_mass_bins = self.checkBoxWakeMassBins.isChecked()

        # Disable/enable inputs if the checkbox is checked/unchecked
        self.checkBoxWakeMassBins.setDisabled(not self.wake_on)
        self.inputWakePlotHt.setDisabled(not self.wake_on)
        self.inputWakePSF.setDisabled(not self.wake_on)
        self.inputWakeExt.setDisabled(not self.wake_on)
        self.radioButtonWakeNormalizationPeak.setDisabled(not self.wake_on)
        self.radioButtonWakeNormalizationArea.setDisabled(not self.wake_on)
        self.wakeSaveVideoButton.setDisabled(not self.wake_on)
        self.radioButtonWakeAlignNone.setDisabled(not self.wake_on)
        self.radioButtonWakeAlignPeak.setDisabled(not self.wake_on)
        self.radioButtonWakeAlignCorrelate.setDisabled(not self.wake_on)

        # Read inputs
        self.readInputBoxes()


    def checkBoxWakeMassBinsSignal(self, event):
        """ Control what happens when the wake checkbox for showing mass bins is toggled. """

        # Read the mass bins show checkbox
        self.wake_show_mass_bins = self.checkBoxWakeMassBins.isChecked()

        # Update the wake plot
        self.updateWakePlot()



    def checkBoxErosionSignal(self, event):
        """ Control what happens when the erosion checkbox is pressed. """

        # Read the wake checkbox
        self.const.erosion_on = self.checkBoxErosion.isChecked()

        # Disable/enable inputs if the checkbox is checked/unchecked
        self.inputErosionHtStart.setDisabled(not self.const.erosion_on)
        self.inputErosionCoeff.setDisabled(not self.const.erosion_on)
        self.inputErosionHtChange.setDisabled(not self.const.erosion_on)
        self.inputErosionCoeffChange.setDisabled(not self.const.erosion_on)
        self.inputErosionRhoChange.setDisabled(not self.const.erosion_on)
        self.inputErosionAblationCoeffChange.setDisabled(not self.const.erosion_on)
        self.inputErosionMassIndex.setDisabled(not self.const.erosion_on)
        self.inputErosionMassMin.setDisabled(not self.const.erosion_on)
        self.inputErosionMassMax.setDisabled(not self.const.erosion_on)

        self.checkBoxErosionRhoSignal(None)
        self.checkBoxErosionAblationCoeffSignal(None)

        # Read inputs
        self.readInputBoxes()



    def checkBoxErosionRhoSignal(self, event):
        """ Use a different erosion coefficient after disruption. """

        self.erosion_different_rho = self.checkBoxErosionRhoChange.isChecked()

        # Disable/enable different density coefficient checkbox
        self.inputErosionRhoChange.setDisabled((not self.erosion_different_rho) \
            or (not self.const.erosion_on))

        # Read inputs
        self.readInputBoxes()


    def checkBoxErosionAblationCoeffSignal(self, event):
        """ Use a different erosion coefficient after disruption. """

        self.erosion_different_sigma = self.checkBoxErosionAblationCoeffChange.isChecked()

        # Disable/enable different ablation coeff checkbox
        self.inputErosionAblationCoeffChange.setDisabled((not self.erosion_different_sigma) \
            or (not self.const.erosion_on))

        # Read inputs
        self.readInputBoxes()



    def checkBoxDisruptionSignal(self, event):
        """ Control what happens when the disruption checkbox is pressed. """

        # Read the wake checkbox
        self.const.disruption_on = self.checkBoxDisruption.isChecked()

        # Disable/enable inputs if the checkbox is checked/unchecked
        self.checkBoxDisruptionErosionCoeff.setDisabled(not self.const.disruption_on)
        self.inputDisruptionErosionCoeff.setDisabled(not self.const.disruption_on)
        self.inputCompressiveStrength.setDisabled(not self.const.disruption_on)
        self.inputDisruptionMassGrainRatio.setDisabled(not self.const.disruption_on)
        self.inputDisruptionMassIndex.setDisabled(not self.const.disruption_on)
        self.inputDisruptionMassMinRatio.setDisabled(not self.const.disruption_on)
        self.inputDisruptionMassMaxRatio.setDisabled(not self.const.disruption_on)

        self.checkBoxDisruptionErosionCoeffSignal(None)

        # Read inputs
        self.readInputBoxes()


    def checkBoxDisruptionErosionCoeffSignal(self, event):
        """ Use a different erosion coefficient after disruption. """

        self.disruption_different_erosion_coeff = self.checkBoxDisruptionErosionCoeff.isChecked()

        # Disable/enable different erosion coefficient checkbox
        self.inputDisruptionErosionCoeff.setDisabled((not self.disruption_different_erosion_coeff) \
            or (not self.const.disruption_on))

        # Read inputs
        self.readInputBoxes()


    def checkBoxFragmentationShowIndividualLCsSignal(self, event):
        """ Toggle computing light curves of individual fragments during complex fragmentation. """

        self.const.fragmentation_show_individual_lcs = self.checkBoxFragmentationShowIndividualLCs.isChecked()


    def checkBoxPlotElectronLineDensitySignal(self, event):
        """ Toggle plotting the electron line density on the magnitude plot. """

        self.electron_density_plot_show = self.checkBoxPlotElectronLineDensity.isChecked()



    def toggleWakeNormalizationMethod(self, event):
        """ Toggle methods of wake plot normalization. """

        if self.radioButtonWakeNormalizationPeak.isChecked():
            
            # Set the normalization method
            self.wake_normalization_method = 'peak'



        if self.radioButtonWakeNormalizationArea.isChecked():
            
            # Set the normalization method
            self.wake_normalization_method = 'area'



        self.updateWakePlot()



    def toggleWakeAlignMethod(self, event):
        """ Toggle method of wake horizontal alignment. """

        if self.radioButtonWakeAlignNone.isChecked():

            # Set the align methods
            self.wake_align_method = 'none'


        if self.radioButtonWakeAlignPeak.isChecked():

            # Set the align methods
            self.wake_align_method = 'peak'


        if self.radioButtonWakeAlignCorrelate.isChecked():

            # Set the align methods
            self.wake_align_method = 'correlate'


        self.updateWakePlot()



    def _tryReadBox(self, input_box, value, value_type=float):
        try:
            value = value_type(input_box.text())
        except:
            msg = QMessageBox()
            msg.setIcon(QMessageBox.Critical)
            msg.setWindowTitle("Input parsing error")
            msg.setText("Error reading input box " + input_box.objectName())
            msg.setInformativeText("Setting it back to: " + str(value))
            msg.exec_()

        return value



    def readInputBoxes(self):
        """ Read input boxes and set values to the Constants object. """

        
        ### Simulation params ###

        self.const.dt = self._tryReadBox(self.inputTimeStep, self.const.dt)
        self.const.P_0m = self._tryReadBox(self.inputP0M, self.const.P_0m)

        self.const.h_init = 1000*self._tryReadBox(self.inputHtInit, self.const.h_init/1000)
        self.const.m_kill = self._tryReadBox(self.inputMassKill, self.const.m_kill)
        self.const.v_kill = 1000*self._tryReadBox(self.inputVelKill, self.const.v_kill/1000)
        self.const.h_kill = 1000*self._tryReadBox(self.inputHtKill, self.const.h_kill/1000)

        ### ###


        ### Meteoroid physical properties ###

        self.const.rho = self._tryReadBox(self.inputRho, self.const.rho)
        self.const.rho_grain = self._tryReadBox(self.inputRhoGrain, self.const.rho_grain)
        self.const.m_init = self._tryReadBox(self.inputMassInit, self.const.m_init)
        self.const.sigma = self._tryReadBox(self.inputAblationCoeff, self.const.sigma*1e6)/1e6
        self.const.v_init = 1000*self._tryReadBox(self.inputVelInit, self.const.v_init/1000)
        self.const.shape_factor = self._tryReadBox(self.inputShapeFact, self.const.shape_factor)
        self.const.gamma = self._tryReadBox(self.inputGamma, self.const.gamma)
        self.const.zenith_angle = np.radians(self._tryReadBox(self.inputZenithAngle, \
            np.degrees(self.const.zenith_angle)))

        # If the bulk density is higher than the grain density, set the grain density to the bulk denisty
        if self.const.rho > self.const.rho_grain:
            self.const.rho_grain = self.const.rho


        self.const.lum_eff = self._tryReadBox(self.inputLumEff, self.const.lum_eff)
        self.const.lum_eff_type = self.lumEffComboBox.currentIndex()

        ### ###


        ### Wake parameters ###

        self.const.wake_psf = self._tryReadBox(self.inputWakePSF, self.const.wake_psf)
        self.const.wake_extension = self._tryReadBox(self.inputWakeExt, self.const.wake_extension)
        self.wake_plot_ht = 1000*self._tryReadBox(self.inputWakePlotHt, self.wake_plot_ht/1000)

        ### ###


        ### Erosion parameters ###

        self.const.erosion_height_start = 1000*self._tryReadBox(self.inputErosionHtStart, \
            self.const.erosion_height_start/1000)
        self.const.erosion_bins_per_10mass = int(self._tryReadBox(self.inputBinsPer10m, \
            self.const.erosion_bins_per_10mass))
        self.const.erosion_coeff = self._tryReadBox(self.inputErosionCoeff, self.const.erosion_coeff*1e6)/1e6
        self.const.erosion_height_change = 1000*self._tryReadBox(self.inputErosionHtChange, \
            self.const.erosion_height_change/1000)
        self.const.erosion_coeff_change = self._tryReadBox(self.inputErosionCoeffChange, \
            self.const.erosion_coeff_change*1e6)/1e6
        self.const.erosion_mass_index = self._tryReadBox(self.inputErosionMassIndex, \
            self.const.erosion_mass_index)
        self.const.erosion_mass_min = self._tryReadBox(self.inputErosionMassMin, self.const.erosion_mass_min)
        self.const.erosion_mass_max = self._tryReadBox(self.inputErosionMassMax, self.const.erosion_mass_max)


        # If a different density value after the change of erosion is used, read it
        if self.erosion_different_rho:
            self.const.erosion_rho_change = self._tryReadBox(self.inputErosionRhoChange, \
                self.const.erosion_rho_change)
        else:
            # Otherwise, use the same bulk density value
            self.const.erosion_rho_change = self.const.rho


        # If a different ablation coeff value after the change of erosion is used, read it
        if self.erosion_different_sigma:
            self.const.erosion_sigma_change = self._tryReadBox(self.inputErosionAblationCoeffChange, \
                self.const.erosion_sigma_change*1e6)/1e6
        else:
            # Otherwise, use the same bulk density value
            self.const.erosion_sigma_change = self.const.sigma

        ### ###



        ### Disruption parameters ###

        self.const.compressive_strength = 1000*self._tryReadBox(self.inputCompressiveStrength, \
            self.const.compressive_strength/1000)

        # If a different value for erosion coefficient after disruption should be used, read it
        if self.disruption_different_erosion_coeff:
            self.const.disruption_erosion_coeff = self._tryReadBox(self.inputDisruptionErosionCoeff, \
                self.const.disruption_erosion_coeff*1e6)/1e6
        else:
            # Otherwise, use the same value
            self.const.disruption_erosion_coeff = self.const.erosion_coeff


        self.const.disruption_mass_grain_ratio = self._tryReadBox(self.inputDisruptionMassGrainRatio, \
            self.const.disruption_mass_grain_ratio*100)/100
        self.const.disruption_mass_index = self._tryReadBox(self.inputDisruptionMassIndex, \
            self.const.disruption_mass_index)
        self.const.disruption_mass_min_ratio = self._tryReadBox(self.inputDisruptionMassMinRatio, \
            self.const.disruption_mass_min_ratio*100)/100
        self.const.disruption_mass_max_ratio = self._tryReadBox(self.inputDisruptionMassMaxRatio, \
            self.const.disruption_mass_max_ratio*100)/100

        ### ###


        ### Autofit parameters ###

        self.pso_iterations = self._tryReadBox(self.inputAutoFitPSOIterations, self.pso_iterations, \
            value_type=int)
        self.pso_particles = self._tryReadBox(self.inputAutoFitPSOParticles, self.pso_particles, \
            value_type=int)

        self.autofit_mag_weight = self._tryReadBox(self.inputAutoFitMagWeight, self.autofit_mag_weight)
        self.autofit_lag_weight_ht_change = 1000*self._tryReadBox(self.inputAutoFitLagWeightHtChange, \
            self.autofit_lag_weight_ht_change/1000)
        self.autofit_lag_weights = self._tryReadBox(self.inputAutoFitLagWeights, self.autofit_lag_weights, \
            value_type=lambda x: list(map(float, x.split(","))))

        ###


        ### Radar measurements ###

        # Height of electron line density measurement
        self.const.electron_density_meas_ht = 1000*self._tryReadBox(self.inputElectronDensityHt, \
            self.const.electron_density_meas_ht/1000)

        # Electron line density measurement
        self.const.electron_density_meas_q = self._tryReadBox(self.inputElectronDensity, \
            self.const.electron_density_meas_q)

        ### ###


        # Update the boxes with read values
        self.updateInputBoxes()



    def calcHeightLimits(self):
        """ Find the upper and lower height limit for all plots. """


        # Track plot limits
        plot_beg_ht = -np.inf
        plot_end_ht = np.inf


        # Keep track of the height range from the original observations
        for obs in self.traj.observations:

            height_data = obs.model_ht[obs.ignore_list == 0]/1000

            # Keep track of the height limits
            plot_beg_ht = max(plot_beg_ht, np.max(height_data))
            plot_end_ht = min(plot_end_ht, np.min(height_data))


        # Track heihgts of additional observations from the .met file (if available)
        if self.met_obs is not None:

            # Plot additional lags for all sites (plot only mirfit lags)
            for site in self.met_obs.sites:

                height_data = self.met_obs.height_data[site]/1000

                # Keep track of the height limits
                plot_beg_ht = max(plot_beg_ht, np.max(height_data))
                plot_end_ht = min(plot_end_ht, np.min(height_data))


        return plot_beg_ht, plot_end_ht





    def updateCommonPlotFeatures(self, plt_handle, sr, plot_text=False):
        """ Update common features on all plots such as the erosion start. 

        Arguments:
            plt_handle: [axis handle]
            sr: [object] Simulation results.
        """


        # Get the plot X limits
        x_min, x_max = plt_handle.get_xlim()

        # Get the plot Y limits
        y_min, y_max = plt_handle.get_ylim()


        # Generate array for horizontal line plotting
        x_arr = np.linspace(x_min, x_max, 10)


        # Plot the beginning only if it's inside the plot and the erosion is on
        if self.const.erosion_on and (self.const.erosion_height_start/1000 >= y_min) \
            and (self.const.erosion_height_start/1000 <= y_max):
            
            # Plot a line marking erosion beginning
            plt_handle.plot(x_arr, np.zeros_like(x_arr) + self.const.erosion_height_start/1000, \
                linestyle='dashed', color='k', alpha=0.25)

            # Add the text about erosion begin
            if plot_text:
                plt_handle.text(x_min, TEXT_LABEL_HT_PAD + self.const.erosion_height_start/1000,\
                    "Erosion beg", size=7, alpha=0.5)



        # Only plot the erosion change if it's above the meteor end and inside the plot
        if (self.const.erosion_height_change > self.traj.rend_ele) and (self.const.erosion_height_change/1000\
            >= y_min) and (self.const.erosion_height_change/1000 <= y_max):

            # Plot a line marking erosion change
            plt_handle.plot(x_arr, np.zeros_like(x_arr) + self.const.erosion_height_change/1000,\
                linestyle='dashed', color='k', alpha=0.25)

            # Add the text about erosion change
            if plot_text:
                plt_handle.text(x_min, TEXT_LABEL_HT_PAD \
                    + self.const.erosion_height_change/1000, "Erosion change", size=7, alpha=0.5)



        # Plot the disruption height
        if self.const.disruption_on and (self.const.disruption_height is not None):

            # Check that the disruption height is inside the plot
            if (self.const.disruption_height/1000 >= y_min) and (self.const.disruption_height/1000 <=y_max):

                plt_handle.plot(x_arr, np.zeros_like(x_arr) + self.const.disruption_height/1000,\
                    linestyle='dotted', color='k', alpha=0.5)

                # Add the text about disruption
                if plot_text:
                    plt_handle.text(x_min, TEXT_LABEL_HT_PAD \
                        + self.const.disruption_height/1000, "Disruption", size=7, alpha=0.5)



        # Force the plot X limits back
        plt_handle.set_xlim([x_min, x_max])




    def updateMagnitudePlot(self, show_previous=False):
        """ Update the magnitude plot. 

        Keyword arguments:
            show_previous: [bool] Show the previous simulation. False by default.

        """

        # Choose to show current or previous results
        if show_previous:
            sr = self.simulation_results_prev
        else:
            sr = self.simulation_results


        self.magnitudePlot.canvas.axes.clear()
        self.electronDensityPlot.clear()

        
        # Track plot limits
        mag_brightest = np.inf
        mag_faintest = -np.inf

        # Plot observed magnitudes from different stations (only if additional LC data is not given)
        if self.lc_data is None:
            for obs in self.traj.observations:

                # Skip instances when no magnitudes are present
                if obs.absolute_magnitudes is None:
                    continue

                # Extract data
                abs_mag_data = obs.absolute_magnitudes[obs.ignore_list == 0]
                height_data = obs.model_ht[obs.ignore_list == 0]/1000

                # Don't plot magnitudes fainter than -10
                mag_filter = abs_mag_data < 10
                height_data = height_data[mag_filter]
                abs_mag_data = abs_mag_data[mag_filter]

                if len(height_data) == 0:
                    continue

                self.magnitudePlot.canvas.axes.plot(abs_mag_data, height_data, marker='x',
                    linestyle='dashed', label=obs.station_id, markersize=5, linewidth=1)

                # Keep track of the faintest and the brightest magnitude
                if len(abs_mag_data):
                    mag_brightest = min(mag_brightest, np.min(abs_mag_data[~np.isinf(abs_mag_data)]))
                    mag_faintest = max(mag_faintest, np.max(abs_mag_data[~np.isinf(abs_mag_data)]))
                else:
                    mag_faintest = 6.0
                    if sr is not None:
                        mag_brightest = np.min(sr.abs_magnitude)
                    else:
                        mag_brightest = -2.0
            


        # Plot additional observations from the .met file (if available)
        if self.met_obs is not None:

            # Plot additional magnitudes for all sites
            for site in self.met_obs.sites:

                # Extract data
                abs_mag_data = self.met_obs.abs_mag_data[site]
                height_data = self.met_obs.height_data[site]/1000

                self.magnitudePlot.canvas.axes.plot(abs_mag_data, \
                    height_data, marker='x', linestyle='dashed', label=str(site), markersize=5, linewidth=1)

                # Keep track of the faintest and the brightest magnitude
                mag_brightest = min(mag_brightest, np.min(abs_mag_data[~np.isinf(abs_mag_data)]))
                mag_faintest = max(mag_faintest, np.max(abs_mag_data[~np.isinf(abs_mag_data)]))


        # Plot additional observations from the light curve CSV file (if available)
        if self.lc_data is not None:

            # Plot additional magnitudes for all sites
            for site in self.lc_data.sites:

                # Extract data
                abs_mag_data = self.lc_data.abs_mag_data[site]
                height_data = self.lc_data.height_data[site]/1000

                # self.magnitudePlot.canvas.axes.plot(abs_mag_data, \
                #     height_data, marker='x', linestyle='dashed', label=str(site), markersize=1, linewidth=1)

                self.magnitudePlot.canvas.axes.plot(abs_mag_data, \
                    height_data, marker='x', linestyle='none', label=str(site), markersize=2, linewidth=1)

                # Keep track of the faintest and the brightest magnitude
                mag_brightest = min(mag_brightest, np.min(abs_mag_data[~np.isinf(abs_mag_data)]))
                mag_faintest = max(mag_faintest, np.max(abs_mag_data[~np.isinf(abs_mag_data)]))


        # Plot simulated magnitudes
        q_sim_plot = None
        q_meas_plot = None
        if sr is not None:

            # # Cut the part with same beginning heights as observations
            # temp_arr = np.c_[sr.brightest_height_arr, sr.abs_magnitude]
            # temp_arr = temp_arr[(sr.brightest_height_arr < plot_beg_ht*1000) \
            #     & (sr.brightest_height_arr > plot_end_ht*1000)]
            # ht_arr, abs_mag_arr = temp_arr.T

            # Plot the simulated magnitudes
            # for ii in range(len(sr.abs_magnitude)):
            #     print("{:}, {:}".format(sr.abs_magnitude[ii], sr.leading_frag_height_arr[ii]/1000))
            self.magnitudePlot.canvas.axes.plot(sr.abs_magnitude, sr.leading_frag_height_arr/1000, \
                label='Simulated', color='k', alpha=0.5)


            if self.electron_density_plot_show:

                # Plot the simulated electron line density
                q_sim_plot = self.electronDensityPlot.plot(np.log10(sr.electron_density_total_arr), \
                    sr.leading_frag_height_arr/1000, color='b', alpha=0.5, linestyle='dashed')

                # Plot the measured electron line density
                if self.const.electron_density_meas_ht > 0:

                    q_meas_plot = self.electronDensityPlot.scatter( \
                        np.log10(self.const.electron_density_meas_q), \
                        self.const.electron_density_meas_ht/1000, c='b', s=10, marker='o')



            # Plot magnitudes of individual fragments
            if self.const.fragmentation_show_individual_lcs:

                # Plot the magnitude of the initial/main fragment
                self.magnitudePlot.canvas.axes.plot(sr.abs_magnitude_main, \
                    sr.leading_frag_height_arr/1000, color='blue', linestyle='solid', linewidth=1, \
                    alpha=0.5)

                # Plot the magnitude of the eroded and disrupted fragments
                self.magnitudePlot.canvas.axes.plot(sr.abs_magnitude_eroded, \
                    sr.leading_frag_height_arr/1000, color='purple', linestyle='dashed', linewidth=1, \
                    alpha=0.5)


                # Plot magnitudes for every fragmentation entry
                for frag_entry in self.const.fragmentation_entries:

                    # Plot magnitude of the main fragment in the fragmentation (not the grains)
                    if len(frag_entry.main_height_data):

                        # Choose the color of the main fragment depending on the fragmentation type
                        if frag_entry.frag_type == "F":
                            line_color = 'blue'
                        elif frag_entry.frag_type == "EF":
                            line_color = 'green'

                        self.magnitudePlot.canvas.axes.plot(frag_entry.main_abs_mag, \
                            frag_entry.main_height_data/1000, color=line_color, linestyle='dashed', \
                            linewidth=1, alpha=0.5)


                    # Plot magnitude of the grains
                    if len(frag_entry.grains_height_data):

                        # Eroding grains are purple, dust grains as red
                        if frag_entry.frag_type == "EF":
                            line_color = 'purple'
                        elif frag_entry.frag_type == "D":
                            line_color = 'red'

                        self.magnitudePlot.canvas.axes.plot(frag_entry.grains_abs_mag, \
                            frag_entry.grains_height_data/1000, color=line_color, linestyle='dashed', \
                            linewidth=1, alpha=0.5)




        # Label for magnitude plot
        self.magnitudePlot.canvas.axes.set_ylabel('Height (km)')
        self.magnitudePlot.canvas.axes.set_xlabel('Abs magnitude')

        # Get legend entries
        handles, labels = self.magnitudePlot.canvas.axes.get_legend_handles_labels()

        # Label for electron line density
        if self.electron_density_plot_show:
            
            self.electronDensityPlot.set_xlabel("log$_{10}$ q (e$^{-}$/m)")

            # Scale the electron line density so it's not in the way of magnitude
            q_min, q_max = self.electronDensityPlot.get_xlim()
            self.electronDensityPlot.set_xlim(q_min, q_max)
            self.electronDensityPlot.set_visible(True)

            # Add to the legend entry
            if q_sim_plot is not None:
                handles += [q_sim_plot[0]]
                labels += ["Sim q"]

            if q_meas_plot is not None:
                handles += [q_meas_plot]
                labels += ["Meas q"]


        else:
            self.electronDensityPlot.set_visible(False)
            self.magnitudePlot.canvas.axes.set_title('Magnitude')


        self.magnitudePlot.canvas.axes.set_ylim([self.plot_end_ht + END_HT_PAD, \
            self.plot_beg_ht + BEG_HT_PAD])
        self.magnitudePlot.canvas.axes.set_xlim([mag_brightest - 1, mag_faintest + 1])
        self.magnitudePlot.canvas.axes.invert_xaxis()

        # Plot common features across all plots
        self.updateCommonPlotFeatures(self.magnitudePlot.canvas.axes, sr, plot_text=True)

        #self.magnitudePlot.canvas.axes.legend(prop={'size': LEGEND_TEXT_SIZE}, loc='upper right')
        self.magnitudePlot.canvas.axes.legend(handles, labels, prop={'size': LEGEND_TEXT_SIZE}, loc='upper right')

        self.magnitudePlot.canvas.axes.grid(color="k", linestyle='dotted', alpha=0.3)

        self.magnitudePlot.canvas.figure.tight_layout()

        self.magnitudePlot.canvas.draw()



    def updateVelocityPlot(self, show_previous=False):
        """ Update the velocity plot. 
        
        Arguments:
            None

        Keyword arguments:
            show_previous: [bool] Show the previous simulation. False by default.

        """

        # Choose to show current or previous results
        if show_previous:
            sr = self.simulation_results_prev
        else:
            sr = self.simulation_results


        self.velocityPlot.canvas.axes.clear()


        vel_min = np.inf
        vel_max = -np.inf

        # Plot observed velocities from different stations
        for obs in self.traj.observations:

            # Extract data
            vel_data = obs.velocities[obs.ignore_list == 0][1:]/1000
            height_data = obs.model_ht[obs.ignore_list == 0][1:]/1000

            self.velocityPlot.canvas.axes.plot(vel_data, height_data, marker='o', label=obs.station_id, \
                markersize=1, linestyle='none')

            # Keep track of the faintest and the brightest magnitude
            vel_min = min(vel_min, np.min(vel_data))
            vel_max = max(vel_max, np.max(vel_data))



        # Plot the observed initial velocity
        self.velocityPlot.canvas.axes.plot(self.traj.orbit.v_init/1000, self.traj.rbeg_ele/1000, \
            marker='x', label="Vinit obs", markersize=5, linestyle='none', color='k')

        # Plot the observed average velocity
        avg_vel_ht_plot_arr = np.linspace(self.traj.rbeg_ele/1000, self.traj.rend_ele/1000, 10)
        self.velocityPlot.canvas.axes.plot(np.zeros_like(avg_vel_ht_plot_arr) \
            + self.traj.orbit.v_avg/1000, avg_vel_ht_plot_arr, label="Vavg obs", linestyle='dashed', \
            color='k', alpha=0.5)



        # Plot simulated velocity
        if sr is not None:

            # Plot the simulated velocity at the brightest point
            self.velocityPlot.canvas.axes.plot(sr.brightest_vel_arr/1000, sr.brightest_height_arr/1000, \
                label='Simulated - brightest', color='k', alpha=0.5)

            # Plot the simulated velocity at the brightest point
            self.velocityPlot.canvas.axes.plot(sr.leading_frag_vel_arr/1000, sr.leading_frag_height_arr/1000,\
                label='Simulated - leading', color='k', alpha=0.5, linestyle="dashed")



            ### Compute the simulated average velocity ###

            # Select only the height range from observations
            sim_vel_obs_range = sr.brightest_vel_arr[(sr.brightest_height_arr <= self.traj.rbeg_ele) \
                & (sr.brightest_height_arr >= self.traj.rend_ele)]

            # Compute the simulated average velocity
            v_avg_sim = np.mean(sim_vel_obs_range)

            ### ###


            # Plot the simulated average velocity
            self.velocityPlot.canvas.axes.plot(np.zeros_like(avg_vel_ht_plot_arr) \
                + v_avg_sim/1000, avg_vel_ht_plot_arr, label="Vavg sim", linestyle='dotted', \
                color='k', alpha=0.5)



        self.velocityPlot.canvas.axes.set_ylabel('Height (km)')
        self.velocityPlot.canvas.axes.set_xlabel('Velocity (km/s)')

        self.velocityPlot.canvas.axes.set_ylim([self.plot_end_ht + END_HT_PAD, self.plot_beg_ht + BEG_HT_PAD])
        self.velocityPlot.canvas.axes.set_xlim([vel_min - 1, vel_max + 1])

        # Plot common features across all plots
        self.updateCommonPlotFeatures(self.velocityPlot.canvas.axes, sr)

        self.velocityPlot.canvas.axes.legend(prop={'size': LEGEND_TEXT_SIZE}, loc='upper left')

        self.velocityPlot.canvas.axes.grid(color="k", linestyle='dotted', alpha=0.3)

        self.velocityPlot.canvas.axes.set_title('Velocity')

        self.velocityPlot.canvas.figure.tight_layout()

        self.velocityPlot.canvas.draw()



    def updateLagPlot(self, show_previous=False):
        """ Update the lag plot. 
        
        Arguments:
            None

        Keyword arguments:
            show_previous: [bool] Show the previous simulation. False by default.

        """

        # Choose to show current of previous results
        if show_previous:
            sr = self.simulation_results_prev
        else:
            sr = self.simulation_results


        self.lagPlot.canvas.figure.clear()


        # Update the observed initial velocity label
        # NOTE: The ECI, not the ground-fixed velocity needs to be used, as the meteor model does not
        #   include Earth's rotation!
        self.vInitObsLabel.setText("Vinit obs = {:.3f} km/s".format(self.traj.orbit.v_init/1000))


        # Compute things needed for the simulated lag
        if sr is not None:

            # Get the model velocity at the observed beginning height
            sim_beg_ht_indx = np.argmin(np.abs(self.traj.rbeg_ele - sr.brightest_height_arr))
            v_init_sim = sr.brightest_vel_arr[sim_beg_ht_indx]

            # Update the simulated initial velocity label
            self.vInitSimLabel.setText("Vinit sim = {:.3f} km/s".format(v_init_sim/1000))


            ### Compute parameters for the brightest point on the trajectory ###

            # Cut the part with same beginning heights as observations
            temp_arr = np.c_[sr.brightest_height_arr, sr.brightest_length_arr]
            temp_arr = temp_arr[(sr.brightest_height_arr <= self.traj.rbeg_ele) \
                & (sr.brightest_height_arr >= self.plot_end_ht)]
            brightest_ht_arr, brightest_len_arr = temp_arr.T

            if len(brightest_len_arr):

                # Compute the simulated lag using the observed velocity
                brightest_lag_sim = brightest_len_arr - brightest_len_arr[0] \
                    - self.traj.orbit.v_init*np.arange(0, self.const.dt*len(brightest_len_arr), \
                                                       self.const.dt)[:len(brightest_len_arr)]

                ###


                ### Compute parameters for the leading point on the trajectory ###

                # Cut the part with same beginning heights as observations
                temp_arr = np.c_[sr.leading_frag_height_arr, sr.leading_frag_length_arr]
                temp_arr = temp_arr[(sr.leading_frag_height_arr <= self.traj.rbeg_ele) \
                    & (sr.leading_frag_height_arr >= self.plot_end_ht)]
                leading_ht_arr, leading_frag_len_arr = temp_arr.T

                # Compute the simulated lag using the observed velocity
                leading_lag_sim = leading_frag_len_arr - leading_frag_len_arr[0] \
                                  - self.traj.orbit.v_init*np.arange(0, self.const.dt*len(leading_frag_len_arr), \
                                                                     self.const.dt)[:len(leading_frag_len_arr)]

                ###

            else:
                sr = None



        ### Lag plot ###

        # Init the lag plot
        lag_plot = self.lagPlot.canvas.figure.add_subplot(1, 2, 1, label='lag')


        # Init the lag residuals plot
        lag_residuals_plot = self.lagPlot.canvas.figure.add_subplot(1, 2, 2, label='lag residuals', \
            sharey=lag_plot)


        # Plot the lag from observations
        for obs in self.traj.observations:

            # Get observed heights
            height_data = obs.model_ht[obs.ignore_list == 0]

            # Plot observed lag
            lag_handle = lag_plot.plot(obs.lag[obs.ignore_list == 0], height_data/1000, marker='x', \
                linestyle='dashed', label=obs.station_id, markersize=3, linewidth=0.5)


            # Plot the lag residuals from simulated
            if sr is not None:

                ### Compute the residuals from simulated, brightest point on the trajectory ###

                # Get simulated lags at the same height as observed
                brightest_interp =  scipy.interpolate.interp1d(-brightest_ht_arr, brightest_lag_sim, \
                    bounds_error=False, fill_value=0)

                obs_height_indices = height_data > np.min(brightest_ht_arr)
                obs_hts = height_data[obs_height_indices]
                brightest_residuals = obs.lag[obs.ignore_list == 0][obs_height_indices] \
                    - brightest_interp(-obs_hts)

                # Plot the lag residuals
                lag_residuals_plot.scatter(brightest_residuals, obs_hts/1000, marker='+', \
                    c=lag_handle[0].get_color(), label="Brightest, {:s}".format(obs.station_id), s=6)

                ### ###


                ### Compute the residuals from simulated, leading point on the trajectory ###

                # Get simulated lags at the same height as observed
                leading_interp =  scipy.interpolate.interp1d(-leading_ht_arr, leading_lag_sim, \
                    bounds_error=False, fill_value=0)
                obs_height_indices = height_data > np.min(leading_ht_arr)
                obs_hts = height_data[obs_height_indices]
                leading_residuals = obs.lag[obs.ignore_list == 0][obs_height_indices] \
                    - leading_interp(-obs_hts)

                # Plot the lag residuals
                lag_residuals_plot.scatter(leading_residuals, obs_hts/1000, marker='s', \
                    c=lag_handle[0].get_color(), label="Leading, {:s}".format(obs.station_id), s=6)

                ### ###



        # Plot additional observations from the .met file (if available)
        if self.met_obs is not None:

            # Plot additional lags for all sites (plot only mirfit lags)
            for site in self.met_obs.sites:

                height_data = self.met_obs.height_data[site]/1000

                # Only plot mirfit lags
                if self.met.mirfit:
                    lag_plot.plot(self.met_obs.lag_data[site], height_data, marker='x', \
                        linestyle='dashed', label=str(site),  markersize=5, linewidth=1)


        # Get X plot limits before the simulated lag is plotted
        x_min, x_max = lag_plot.get_xlim()



        # Plot simulated lag
        if sr is not None:


            # Plot lag of the brightest point on the trajectory
            lag_plot.plot(brightest_lag_sim[:len(brightest_ht_arr)], \
                         (brightest_ht_arr/1000)[:len(brightest_lag_sim)], \
                         label='Simulated - brightest', color='k', alpha=0.5)


            # Plot lag of the leading fragment
            lag_plot.plot(leading_lag_sim[:len(leading_ht_arr)], (leading_ht_arr/1000)[:len(leading_lag_sim)], 
                label='Simulated - leading', color='k', alpha=0.5, linestyle='dashed')



        # Set parameters for the lag plot
        lag_plot.set_ylim([self.plot_end_ht + END_HT_PAD, self.plot_beg_ht + BEG_HT_PAD])
        lag_plot.set_xlim([x_min, x_max])

        lag_plot.set_xlabel('Lag (m)')
        lag_plot.set_ylabel('Height (km)')
        
        # Plot common features across all plots
        self.updateCommonPlotFeatures(lag_plot, sr)
        self.updateCommonPlotFeatures(lag_residuals_plot, sr)

        lag_plot.legend(prop={'size': LEGEND_TEXT_SIZE})
        lag_plot.grid(color="k", linestyle='dotted', alpha=0.3)
        lag_plot.set_title('Lag')


        lag_residuals_plot.set_xlabel('Residuals (m)')
        lag_residuals_plot.get_yaxis().set_visible(False)
        lag_residuals_plot.legend(prop={'size': LEGEND_TEXT_SIZE})
        lag_residuals_plot.grid(color="k", linestyle='dotted', alpha=0.3)
        lag_residuals_plot.set_title('Lag residuals')


        self.lagPlot.canvas.figure.tight_layout()
        self.lagPlot.canvas.figure.subplots_adjust(wspace=0)
        self.lagPlot.canvas.draw()



    def incrementWakePlotHeight(self):
        """ Increment wake plot height by 100 m. """

        # If the wake files are loaded, use the wake heights
        if self.wake_heights is not None:
            
            self.wake_ht_current_index = (self.wake_ht_current_index + 1)%len(self.wake_heights)

            # Extract the wake height and observations
            self.wake_plot_ht, self.current_wake_container = self.wake_heights[self.wake_ht_current_index]


        # Otherwise, increment the wake height by 100 m
        else:
            self.wake_plot_ht += 100

        self.updateInputBoxes()
        self.updateWakePlot()



    def decrementWakePlotHeight(self):
        """ Decrement wake plot height by 100 m. """

        # If the wake files are loaded, use the wake heights
        if self.wake_heights is not None:
            
            self.wake_ht_current_index = (self.wake_ht_current_index - 1)%len(self.wake_heights)

            # Extract the wake height and observations
            self.wake_plot_ht, self.current_wake_container = self.wake_heights[self.wake_ht_current_index]


        # Otherwise, decrement the wake height by 100 m
        else:
            self.wake_plot_ht -= 100

        self.updateInputBoxes()
        self.updateWakePlot()




    def redrawWakeHeightLine(self, plt_handle, line_handles, label_handles=None, draw_label=False):
        """ Plot lines on mag, vel, lag plots indicating where the wake is shown. The line will be just 
            refreshed to prevent redrawing all plots from scratch.
        """

        new_line_handles = []
        new_label_handles = []

        # Go through all axes on the image
        for i, ax in enumerate(plt_handle.canvas.figure.get_axes()):

            # Extract line and label handles
            line_handle = None
            if line_handles is not None:
                if i <= (len(line_handles) - 1):
                    line_handle = line_handles[i]

            label_handle = None
            if label_handles is not None:
                if i <= (len(label_handles) - 1):
                    label_handle = label_handles[i]


            # Remove the line from the plot
            if line_handle is not None:
                try:
                    line_handle.remove()
                except ValueError:
                    pass



            # Get the plot limits
            x_min, x_max = ax.get_xlim()
            y_min, y_max = ax.get_ylim()

            # Construct X array
            x_arr = np.linspace(x_min, x_max)

            # Plot the wake line
            line_handle = ax.plot(x_arr, np.zeros_like(x_arr) + self.wake_plot_ht/1000, \
                linestyle='dashed', color='green', alpha=0.5)[0]

            new_line_handles.append(line_handle)



            # Plot the label if given
            if draw_label:

                # Remove the old label handle
                if label_handle is not None:
                    try:
                        label_handle.remove()
                    except ValueError:
                        pass

                # Draw the wake line label
                label_handle = ax.text(x_min, TEXT_LABEL_HT_PAD + self.wake_plot_ht/1000, \
                    "Wake plot", size=7, color='green', alpha=0.5)

                new_label_handles.append(label_handle)


            # Keep the plot limits
            ax.set_xlim([x_min, x_max])
            ax.set_ylim([y_min, y_max])


        # Redraw the plot
        plt_handle.canvas.draw_idle()


        return new_line_handles, new_label_handles



    def normalizeObservedWake(self, len_array, wake_intensity_array, wake_sim, simulated_peak_luminosity, 
        simulated_integrated_luminosity, simulated_peak_length, sim_wake_exists=False):
        """ Normalize the observed wake and match it to the simulated wake. """

        # Normalize the wake by areas under both curves
        if self.wake_normalization_method == "area":

            # Compute the area under the observed wake curve that is within the simulated range
            #   (take only the part after the leading fragment)
            if sim_wake_exists:
                selected_indices = (len_array > -50) | (len_array < self.const.wake_extension)
                wake_intensity_array_trunc = wake_intensity_array[selected_indices]
                len_array_trunc = len_array[selected_indices]
            else:
                wake_intensity_array_trunc = wake_intensity_array
                len_array_trunc = len_array

            observed_integrated_luminosity = np.trapz(wake_intensity_array_trunc, len_array_trunc)


            # Normalize the wake intensity by the area under the intensity curve
            wake_intensity_array *= simulated_integrated_luminosity/observed_integrated_luminosity


        # Normalize the wake by the peak intensity
        else:

            # Handle cases when the simulation doesn't exist
            if simulated_peak_luminosity is None:
                simulated_peak_luminosity = 0

            wake_intensity_array *= simulated_peak_luminosity/np.max(wake_intensity_array)


        # Perform alignments when simulations are available
        if wake_sim is not None:

            # Align wake by peaks
            if self.wake_align_method == 'peak':

                # Find the length of the peak intensity
                peak_len = len_array[np.argmax(wake_intensity_array)]

                # Offset lengths
                len_array -= peak_len + simulated_peak_length


            # Align the wake by cross correlation
            elif self.wake_align_method == 'correlate':


                # Interpolate the model values and sample them at observed points
                sim_wake_interp = scipy.interpolate.interp1d(wake_sim.length_array, \
                    wake_sim.wake_luminosity_profile, bounds_error=False, fill_value=0)
                model_wake_obs_len_sample = sim_wake_interp(-len_array)

                # Correlate the wakes and find the shift
                wake_shift = np.argmax(np.correlate(model_wake_obs_len_sample, wake_intensity_array, \
                    "full")) + 1

                # Find the index of the zero observed length
                obs_len_zero_indx = np.argmin(np.abs(len_array))

                # Add the offset to the observed length
                len_array += len_array[(obs_len_zero_indx + wake_shift)%len(model_wake_obs_len_sample)]


        return len_array, wake_intensity_array



    def updateWakePlot(self, show_previous=False):
        """ Plot the wake. """

        if not show_previous:
            self.readInputBoxes()


        # Choose to show current of previous results
        if show_previous:
            sr = self.simulation_results_prev
        else:
            sr = self.simulation_results


        wake = None


        # Plot lines on mag, vel, lag plots indicating where the wake is shown
        self.magnitudePlotWakeLines, self.magnitudePlotWakeLineLabels \
            = self.redrawWakeHeightLine(self.magnitudePlot, self.magnitudePlotWakeLines, \
                label_handles=self.magnitudePlotWakeLineLabels, draw_label=True)
        self.velocityPlotWakeLines, _ = self.redrawWakeHeightLine(self.velocityPlot, \
            self.velocityPlotWakeLines)
        self.lagPlotWakeLines, _ = self.redrawWakeHeightLine(self.lagPlot, self.lagPlotWakeLines)



        # Find the appropriate observed wake to plot for the given height
        if self.wake_heights is not None:

            # Find the index of the observed wake that's closest to the given plot height
            self.wake_ht_current_index = np.argmin(np.abs(np.array([w[0] for w in self.wake_heights]) \
                - self.wake_plot_ht))

            # Extract the wake height and observations
            self.wake_plot_ht, self.current_wake_container = self.wake_heights[self.wake_ht_current_index]



        self.wakePlot.canvas.figure.clf()


        # Create one large wake plot with movable height (first column)
        wake_ht_plot = self.wakePlot.canvas.figure.add_subplot(1, 2, 1, label='wake ht')


        ### PLOT SIMULATED WAKE ###
        simulated_integrated_luminosity = 1.0
        simulated_peak_luminosity = 1.0
        simulated_peak_length = 0.0
        sim_wake_exists = False
        if sr is not None:

            # Find the wake index closest to the given wake height
            wake_res_indx =  np.argmin(np.abs(self.wake_plot_ht - sr.brightest_height_arr))

            # Get the approprate wake results
            wake = sr.wake_results[wake_res_indx]

            if wake is not None:

                sim_wake_exists = True

                ### Plot the simulated wake ###
                wake_ht_plot.plot(wake.length_array, wake.wake_luminosity_profile, \
                    label='Simulated', color='k', alpha=0.5)

                # Compute the area under the simulated wake curve (take only the part after the leading fragment)
                selected_indices = (wake.length_array > 0) | (wake.length_array < self.const.wake_extension)
                wake_intensity_array_trunc = wake.wake_luminosity_profile[selected_indices]
                len_array_trunc = wake.length_array[selected_indices]
                simulated_integrated_luminosity = np.trapz(wake_intensity_array_trunc, len_array_trunc)

                # Store the max values
                simulated_peak_luminosity = np.max(wake_intensity_array_trunc)
                simulated_peak_length = len_array_trunc[np.argmax(wake_intensity_array_trunc)]

                ### ###


                ### Plot fragment masses ###
                if self.wake_show_mass_bins:

                    # Add second scale for masses (log)
                    mass_ax = wake_ht_plot.twinx()
                    mass_ax.set_yscale("log")

                    frag_main = None
                    frag_len_list = []
                    #m_init_list = []
                    m_list = []
                    for frag in wake.frag_list:

                        if frag.main:
                            frag_main = frag

                        if frag.m > 1.0e-11:

                            # Compute the fragment length on the wake plot
                            frag_wake_len = frag.length - wake.leading_frag_length

                            frag_len_list.append(frag_wake_len)
                            #m_init_list.append(frag.m_init)
                            m_list.append(frag.m)


                    # Plot the mass of the main fragment
                    if frag_main is not None:
                        # mass_ax.scatter(frag_main.length - wake.leading_frag_length, frag_main.m_init, c='k', \
                        #     s=10, alpha=0.5)
                        mass_ax.scatter(frag_main.length - wake.leading_frag_length, frag_main.m, c='r', \
                            s=20, alpha=0.5)


                    # # Plot the fragment initial mass at the position of the fragment length
                    # mass_ax.scatter(frag_len_list, m_init_list, c='k', s=0.5, label="$m_0$")

                    # Plot the instantaneous fragment mass
                    mass_ax.scatter(frag_len_list, m_list, c='r', s=0.5, label="$m$")


                    # Set the mass range (from ablation limit to initial mass)
                    mass_ax.set_ylim([1.0e-11, self.const.m_init])

                    mass_ax.set_ylabel("Fragment mass (kg)")



                ### ###


        ### ###


        ### PLOT OBSERVED WAKE ###

        if self.current_wake_container is not None:
            
            # Extract array of length and intensity 
            len_array = []
            wake_intensity_array = []
            for wake_pt in self.current_wake_container.points:
                len_array.append(wake_pt.leading_frag_length)
                wake_intensity_array.append(wake_pt.intens_sum)

            len_array = np.array(len_array)
            wake_intensity_array = np.array(wake_intensity_array)


            # # Smooth the wake intensity to reduce noise
            # wake_intensity_array = scipy.signal.savgol_filter(wake_intensity_array, 5, 3)


            # Normalize and align the observed wake with simulations
            len_array, wake_intensity_array = self.normalizeObservedWake(len_array, wake_intensity_array, \
                wake, simulated_peak_luminosity, simulated_integrated_luminosity, simulated_peak_length, \
                sim_wake_exists=sim_wake_exists)



            # Plot the observed wake
            wake_ht_plot.plot(-len_array, wake_intensity_array,
                label='Observed, site: {:s}'.format(str(self.current_wake_container.site_id)), color='k', \
                linestyle='dotted')

        ### ###

        if wake_ht_plot.lines or wake_ht_plot.collections:
            wake_ht_plot.legend(prop={'size': LEGEND_TEXT_SIZE})


        wake_ht_plot.set_xlabel('Length behind leading fragment (m)')
        wake_ht_plot.set_ylabel('Intensity')

        wake_ht_plot.invert_xaxis()

        wake_ht_plot.set_ylim(bottom=0)

        wake_ht_plot.set_title('Wake at {:.2f} km'.format(self.wake_plot_ht/1000))




        ### PLOT WAKE OVERVIEW SUBPLOT ###

        # Init second subplot
        wake_overview_plot = self.wakePlot.canvas.figure.add_subplot(1, 2, 2, label='wake overview')

        # Number of wakes shown on the plot
        n_plots = 10


        # Generate a range of heights used for plotting
        if self.wake_heights is not None:

            # Choose heights from observed list of heights
            step = len(self.wake_heights)//n_plots
            wake_plotting_heights = np.array(self.wake_heights[::-step])[:, 0]

        else:
            # Generate a list of heights between the begin and end of the trajectory
            wake_plotting_heights = np.linspace(self.traj.rbeg_ele, self.traj.rend_ele, n_plots)
        

        # Go through different heights
        for i, plot_ht in enumerate(sorted(wake_plotting_heights)):

            plot_shift = 0.75*i

            wake = None
            if sr is not None:

                # Find the wake index closest to the given wake height
                wake_res_indx =  np.argmin(np.abs(plot_ht - sr.brightest_height_arr))

                # Get the approprate wake results
                wake = sr.wake_results[wake_res_indx]


            # Plot the simulated wake
            simulated_integrated_luminosity = 1.0
            simulated_peak_luminosity = None
            simulated_peak_length = 0.0
            sim_wake_exists = False
            if wake is not None:

                sim_wake_exists = True

                # Scale the luminosity profile to 1 and shift
                luminosity_profile_scaled = plot_shift \
                    + wake.wake_luminosity_profile/np.max(wake.wake_luminosity_profile)

                # Plot the simulated wake and scale it to the maximum lumino
                wake_overview_plot.plot(wake.length_array, luminosity_profile_scaled, \
                    label='Simulated', color='k', alpha=0.5, linewidth=1)


                # Add text indicating the height of the wake
                wake_overview_plot.text(wake.length_array[0], luminosity_profile_scaled[0] - 0.1/n_plots,\
                    "{:.2f} km".format(plot_ht/1000), ha='center', va='top', size=6)

                # Compute the area under the simulated wake curve (take only the part after the leading fragment)
                selected_indices = (wake.length_array > 0) | (wake.length_array < self.const.wake_extension)
                wake_intensity_array_trunc = wake.wake_luminosity_profile[selected_indices]
                len_array_trunc = wake.length_array[selected_indices]
                simulated_integrated_luminosity = np.trapz(wake_intensity_array_trunc, len_array_trunc)

                # Store the max values
                simulated_peak_luminosity = np.max(wake_intensity_array_trunc)
                simulated_peak_length = len_array_trunc[np.argmax(wake_intensity_array_trunc)]

                ### ###



            # Plot observed wake
            if self.wake_heights is not None:

                # Find the index of the observed wake that's closest to the given plot height
                wake_ht_index = np.argmin(np.abs(np.array([w[0] for w in self.wake_heights]) \
                    - plot_ht))

                # Extract the wake observations
                _, wake_container = self.wake_heights[wake_ht_index]

                len_array = []
                wake_intensity_array = []
                for wake_pt in wake_container.points:
                    len_array.append(wake_pt.leading_frag_length)
                    wake_intensity_array.append(wake_pt.intens_sum)

                len_array = np.array(len_array)
                wake_intensity_array = np.array(wake_intensity_array)


                # Normalize and align the observed wake with simulations
                len_array, wake_intensity_array = self.normalizeObservedWake(len_array, \
                    wake_intensity_array, wake, simulated_peak_luminosity, \
                    simulated_integrated_luminosity, simulated_peak_length, \
                    sim_wake_exists=sim_wake_exists)


                # Normalize the observed wake to 1
                if simulated_peak_luminosity is not None:
                    obs_wake_scale = simulated_peak_luminosity
                else:
                    obs_wake_scale = np.max(wake_intensity_array)

                wake_intensity_array_scaled = plot_shift + wake_intensity_array/obs_wake_scale

                # Plot the observed wake
                wake_overview_plot.plot(-len_array, wake_intensity_array_scaled, color='k', \
                    linestyle='dotted', linewidth=1)


                # If the simulations are not shown, plot the wake heights
                if wake is None:
                    wake_overview_plot.text(-self.const.wake_extension, wake_intensity_array_scaled[-1] \
                        - 0.1/n_plots, "{:.2f} km".format(plot_ht/1000), ha='center', va='top', size=6)



        wake_overview_plot.set_xlabel('Length behind leading fragment (m)')
        wake_overview_plot.get_yaxis().set_visible(False)

        wake_overview_plot.set_ylim(bottom=0)
        wake_overview_plot.set_xlim(-self.const.wake_extension - 50, 50)

        wake_overview_plot.invert_xaxis()

        wake_overview_plot.set_title('Wake overview')

        ###



        self.wakePlot.canvas.figure.tight_layout()
        self.wakePlot.canvas.draw()



        # Enable/disable wake normalization and alignment dpending on availability of simulated data
        self.wakeNormalizeGroup.setDisabled(wake is None)
        self.wakeAlignGroup.setDisabled(wake is None)



    def showPreviousResults(self):
        """ Show previous simulation results and parameters. """

        if self.simulation_results_prev is not None:

            self.updateInputBoxes(show_previous=True)
            self.updateMagnitudePlot(show_previous=True)
            self.updateVelocityPlot(show_previous=True)
            self.updateLagPlot(show_previous=True)
            self.updateWakePlot(show_previous=True)



    def showCurrentResults(self):
        """ Show current simulation results and parameters. """

        self.updateInputBoxes(show_previous=False)
        self.updateMagnitudePlot(show_previous=False)
        self.updateVelocityPlot(show_previous=False)
        self.updateLagPlot(show_previous=False)
        self.updateWakePlot(show_previous=False)



    def plotAtmDensity(self):
        """ Open a separate plot showing the MSISE air density and the polynomial fit. """

        # Init a matplotlib popup window
        self.mpw = MatplotlibPopupWindow()

        # Set the window title
        self.mpw.setWindowTitle("Air density fit")



        # Take mean meteor lat/lon as reference for the atmosphere model
        lat_mean = np.mean([self.traj.rbeg_lat, self.traj.rend_lat])
        lon_mean = meanAngle([self.traj.rbeg_lon, self.traj.rend_lon])


        # Generate a height array
        height_arr = np.linspace(self.dens_fit_ht_beg, self.dens_fit_ht_end, 200)

        # Get atmosphere densities from NRLMSISE-00 (use log values for the fit)
        atm_densities = np.array([getAtmDensity(lat_mean, lon_mean, ht, self.traj.jdt_ref) \
            for ht in height_arr])


        # Get atmosphere densities from the fitted polynomial
        atm_densities_poly = atmDensPoly(height_arr, self.const.dens_co)


        # Plot the MSISE density
        self.mpw.canvas.axes.semilogx(atm_densities, height_arr/1000, \
            label="NRLMSISE-00", color='k')

        # Poly poly fit
        self.mpw.canvas.axes.semilogx(atm_densities_poly, height_arr/1000, \
            label="Polynomial fit", color='red', linestyle="dashed")


        self.mpw.canvas.axes.legend()

        self.mpw.canvas.axes.set_ylabel("Height (km)")
        self.mpw.canvas.axes.set_xlabel("Atmosphere mass density (kg/m^3)")

        self.mpw.canvas.axes.set_ylim([self.dens_fit_ht_end/1000, self.dens_fit_ht_beg/1000])

        self.mpw.canvas.axes.grid()

        self.mpw.canvas.figure.tight_layout()
            
        self.mpw.show()


    def plotDynamicPressure(self):
        """ Open a separate plot with the simulated dynamic pressure. """


        if self.simulation_results is not None:

            # Init a matplotlib popup window
            self.mpw = MatplotlibPopupWindow()

            # Set the window title
            self.mpw.setWindowTitle("Dynamic pressure")



            sr = self.simulation_results

            # Take mean meteor lat/lon as reference for the atmosphere model
            lat_mean = np.mean([self.traj.rbeg_lat, self.traj.rend_lat])
            lon_mean = meanAngle([self.traj.rbeg_lon, self.traj.rend_lon])

            # Compute the dynamic pressure
            brightest_dyn_pressure = dynamicPressure(lat_mean, lon_mean, sr.brightest_height_arr, \
                self.traj.jdt_ref, sr.brightest_vel_arr, gamma=self.const.gamma)
            leading_frag_dyn_pressure = dynamicPressure(lat_mean, lon_mean, sr.leading_frag_height_arr, \
                self.traj.jdt_ref, sr.leading_frag_vel_arr, gamma=self.const.gamma)


            # Plot dyn pressure
            self.mpw.canvas.axes.plot(brightest_dyn_pressure/1e6, sr.brightest_height_arr/1000, \
                label="Brightest", color='k', alpha=0.5)
            self.mpw.canvas.axes.plot(leading_frag_dyn_pressure/1e6, sr.leading_frag_height_arr/1000, \
                label="Leading", color='k', alpha=0.5, linestyle="dashed")


            # Compute and mark peak on the graph
            brightest_peak_dyn_pressure_index = np.argmax(brightest_dyn_pressure)
            peak_dyn_pressure = brightest_dyn_pressure[brightest_peak_dyn_pressure_index]/1e6
            peak_dyn_pressure_ht = sr.brightest_height_arr[brightest_peak_dyn_pressure_index]/1000
            self.mpw.canvas.axes.scatter(peak_dyn_pressure, peak_dyn_pressure_ht, \
                label="Peak P = {:.2f} MPa\nHt = {:.2f} km".format(peak_dyn_pressure, peak_dyn_pressure_ht))



            self.mpw.canvas.axes.legend()

            self.mpw.canvas.axes.set_ylabel("Height (km)")
            self.mpw.canvas.axes.set_xlabel("Dynamic pressure (MPa)")

            self.mpw.canvas.axes.set_ylim([self.plot_end_ht + END_HT_PAD, self.plot_beg_ht + BEG_HT_PAD])

            self.mpw.canvas.axes.grid()

            self.mpw.canvas.figure.tight_layout()
                
            self.mpw.show()


        else:
            print("No simulation results to show!")



    def plotMassLoss(self):
        """ Open a separate plot with the simulated mass loss. """


        if self.simulation_results is not None:

            # Init a matplotlib popup window
            self.mpw = MatplotlibPopupWindow()

            # Set the window title
            self.mpw.setWindowTitle("Mass loss")



            sr = self.simulation_results

            # Take mean meteor lat/lon as reference for the atmosphere model
            lat_mean = np.mean([self.traj.rbeg_lat, self.traj.rend_lat])
            lon_mean = meanAngle([self.traj.rbeg_lon, self.traj.rend_lon])

            # Compute the dynamic pressure
            brightest_dyn_pressure = dynamicPressure(lat_mean, lon_mean, sr.brightest_height_arr, \
                self.traj.jdt_ref, sr.brightest_vel_arr, gamma=self.const.gamma)
            main_dyn_pressure = dynamicPressure(lat_mean, lon_mean, sr.main_height_arr, \
                self.traj.jdt_ref, sr.main_vel_arr, gamma=self.const.gamma)


            # Filter out very small masses
            mass_filter = sr.mass_total_active_arr > 0.001


            # Plot mass of all fragments
            self.mpw.canvas.axes.loglog(brightest_dyn_pressure[mass_filter]/1e6, \
                sr.mass_total_active_arr[mass_filter], label="Total mass", color='k', linewidth=1, \
                linestyle='dashed')

            # Plot mass of the main fragment
            self.mpw.canvas.axes.loglog(main_dyn_pressure[mass_filter]/1e6, sr.main_mass_arr[mass_filter], \
                label="Main mass", color='k', linewidth=2)

            self.mpw.canvas.axes.set_xlim(xmin=1e-3)


            self.mpw.canvas.axes.legend()

            self.mpw.canvas.axes.set_ylabel("Mass (kg)")
            self.mpw.canvas.axes.set_xlabel("Dynamic pressure (MPa)")

            self.mpw.canvas.axes.grid()

            self.mpw.canvas.figure.tight_layout()
                
            self.mpw.show()


        else:
            print("No simulation results to show!")



    def runSimulationGUI(self):


        # If the fragmentation is turned on and no fragmentation data is given, notify the user
        if self.const.fragmentation_on and (self.fragmentation is None):
            frag_error_message = QMessageBox(QMessageBox.Critical, "Fragmentation file error", \
                "Fragmentation is enabled but no fragmentation file is set.")
            frag_error_message.setInformativeText("Either load an existing fragmentation file or create a new one.")
            frag_error_message.exec_()
            return None


        # Load fragmentation entries if fragmentation is enabled
        if self.const.fragmentation_on:

            # Load the file
            self.fragmentation.loadFragmentationFile()

            # Sort entries by height
            self.fragmentation.sortByHeight()

            # Reset the status of all fragmentations
            self.fragmentation.resetAll()

            # Write the fragmentation file
            self.fragmentation.writeFragmentationFile()


        # Store previous run results
        self.const_prev = copy.deepcopy(self.const)
        self.simulation_results_prev = copy.deepcopy(self.simulation_results)


        # Read the values from the input boxes
        self.readInputBoxes()


        # Disable the simulation button (have to force update by calling "repaint")
        self.runSimButton.setStyleSheet("background-color: red")
        self.runSimButton.setDisabled(True)
        self.repaint()


        print('Running simulation...')
        t1 = time.time()

        # Run the simulation
        frag_main, results_list, wake_results = runSimulation(self.const, compute_wake=self.wake_on)

        sim_runtime = time.time() - t1

        if sim_runtime < 0.5:
            print('Simulation runtime: {:d} ms'.format(int(1000*sim_runtime)))
        elif sim_runtime < 100:
            print('Simulation runtime: {:.2f} s'.format(sim_runtime))
        else:
            print('Simulation runtime: {:.2f} min'.format(sim_runtime/60))

        # Store simulation results
        self.simulation_results = SimulationResults(self.const, frag_main, results_list, wake_results)

        # Write results in the fragmentation file
        if self.const.fragmentation_on:
            self.fragmentation.writeFragmentationFile()

        # Update the plots
        self.showCurrentResults()

        # Save the latest run parameters
        self.saveFitParameters(False, suffix="_latest")

        # Enable the simulation button
        self.runSimButton.setDisabled(False)
        self.runSimButton.setStyleSheet("background-color: #b1eea6")



    def autoFitMethodToggle(self, text):
        """ Select method of auto fit. """

        self.autofit_method = text        

        # Enable/disable PSO options
        self.inputAutoFitPSOIterations.setDisabled("PSO" not in text)
        self.inputAutoFitPSOParticles.setDisabled("PSO" not in text)




    def autoFit(self):
        """ Run the auto fit procedure. """

        # Read inputs
        self.readInputBoxes()


        # Disable the fit button (have to force update by calling "repaint")
        self.autoFitButton.setStyleSheet("background-color: red")
        self.autoFitButton.setDisabled(True)
        self.repaint()


        print()
        print("===================")
        print("AUTO FIT REFINEMENT")
        print("===================")
        print()


        # Store original constants
        const_original = copy.deepcopy(self.const)

        # Save current simulation results
        simulation_results_prefit = copy.deepcopy(self.simulation_results)

        ### Construct input data matrix ###

        # Construct 2D array of height vs. absolute magnitude vs. lag
        temp_list = []
        for obs in self.traj.observations:


            # Extract data
            height_data = obs.model_ht[obs.ignore_list == 0]
            lag_data = obs.lag[obs.ignore_list == 0]


            # If the magnitudes are None, only fit the lag
            if obs.absolute_magnitudes is None:
                abs_mag_data = np.zeros_like(height_data) + np.nan

            else:
                abs_mag_data = obs.absolute_magnitudes[obs.ignore_list == 0]    


            temp_arr = np.c_[height_data, abs_mag_data, lag_data]

            # Skip infinite magnitude
            temp_arr = temp_arr[~np.isinf(abs_mag_data)]

            temp_list.append(temp_arr)


        # Add data from the met file (if available)
        if self.met_obs is not None:

            # Plot additional magnitudes for all sites
            for site in self.met_obs.sites:

                # Extract data
                height_data = self.met_obs.height_data[site]
                abs_mag_data = self.met_obs.abs_mag_data[site]                


                # Add dummy lag data
                lag_data = np.zeros_like(height_data) + np.nan

                temp_arr = np.c_[height_data, abs_mag_data, lag_data]

                # Skip infinite magnitude
                temp_arr = temp_arr[~np.isinf(abs_mag_data)]

                temp_list.append(temp_arr)


        # Construct single data matrix and sort by reverse height
        fit_input_data = np.vstack(temp_list)
        fit_input_data = fit_input_data[np.argsort(fit_input_data[:, 0])[::-1]]


        ### ###


        ### Only fit parameters of fragmentation processes which are used ###

        p0 = []
        bounds = []
        param_string = 'm'
        const = copy.deepcopy(self.const)


        # Select meteoroid parameters
        meteoroid_params = [const.m_init, const.v_init, const.rho, const.sigma]
        meteoroid_bounds = [[0.1*const.m_init, 2*const.m_init],
                            [0.9*const.v_init, 1.1*const.v_init],
                            [100, 6000],
                            [0.25*const.sigma, 4*const.sigma]]

        p0 += meteoroid_params
        bounds += meteoroid_bounds


        # Add erosion parameters if the erosion is on
        if const.erosion_on:

            erosion_params = [const.erosion_height_start, const.erosion_coeff, \
                const.erosion_height_change, const.erosion_coeff_change, \
                const.erosion_mass_index, const.erosion_mass_min, const.erosion_mass_max]

            erosion_bounds = [[70000, 130000],
                              [0.0, 5.0/1e6],
                              [70000, 130000],
                              [0.0, 5.0/1e6],
                              [1.0, 3.0],
                              [1e-12, 1e-9],
                              [1e-11, 1e-7]]

            p0 += erosion_params
            bounds += erosion_bounds

            param_string += 'e'


        # Add distuption parameters if the disruption is on
        if const.disruption_on:

            disruption_params = [const.compressive_strength, const.disruption_erosion_coeff, \
                const.disruption_mass_index, const.disruption_mass_min_ratio, \
                const.disruption_mass_max_ratio, const.disruption_mass_grain_ratio]

            disruption_bounds = [[0.1*const.compressive_strength, 10*const.compressive_strength],
                                 [0.0, 1.0/1e6],
                                 [1.0, 3.0],
                                 [0.01, 50.0],
                                 [0.1, 80.0],
                                 [0.0, 100.0]]

            p0 += disruption_params
            bounds += disruption_bounds

            param_string += 'd'


        ### ###



        # Normalize the fit parameters to the [0, 1] range
        mini_norm_handle = MinimizationParameterNormalization(p0)
        p0_normed, bounds_normed = mini_norm_handle.normalizeBounds(bounds)


        # Print residual value
        print("Starting residual value: {:.5f}".format(fitResiduals(p0_normed, fit_input_data, \
            param_string, const, self.traj, mini_norm_handle, mag_weight=self.autofit_mag_weight, \
            lag_weights=self.autofit_lag_weights, lag_weight_ht_change=self.autofit_lag_weight_ht_change, \
            verbose=False)))
        print()



        print("Method:", self.autofit_method)


        if self.autofit_method == "Local":

            ### scipy minimize ###

            # Run the fit
            res = scipy.optimize.minimize(fitResiduals, p0_normed, args=(fit_input_data, param_string, \
                const, self.traj, mini_norm_handle, self.autofit_mag_weight, self.autofit_lag_weights, \
                self.autofit_lag_weight_ht_change, True, self), bounds=bounds_normed, tol=0.001)

            print(res)

            fit_params = res.x


            ### ###


        # PSO optimization
        else:

            print()
            print("N particles:", self.pso_particles)
            print("Iterations:", self.pso_iterations)

            ### pyswarms ###

            import pyswarms as ps
            from pyswarms.utils.plotters import plot_cost_history


            # Set up hyperparameters
            #options = {'c1': 0.5, 'c2': 0.7, 'w':0.9}
            options = {'c1': 0.6, 'c2': 0.3, 'w': 0.9, 'k': 10, 'p': 1}


            # Set up bounds (min, max) are (0, 1)
            pso_bounds = (np.zeros(len(p0_normed)), np.ones(len(p0_normed)))


            init_pos = None


            # If PSO local optimization is desired, create a tight cluster of particles around the initial
            #   parameters
            if self.autofit_method == "PSO local":

                # Create particles in a tight Gaussian around the initial parameters
                init_pos = np.random.normal(loc=p0_normed, scale=0.2 + np.zeros_like(p0_normed), \
                    size=(self.pso_particles - 1, len(p0_normed)))
                init_pos[init_pos < 0] = abs(init_pos[init_pos < 0])
                init_pos[init_pos > 1] = 1 - init_pos[init_pos > 1] + 1

                # Add manual fit to initial positions
                init_pos = np.append(init_pos, np.array([p0_normed]), axis=0)


            # Call instance of PSO with bounds argument
            optimizer = ps.single.LocalBestPSO(n_particles=self.pso_particles, dimensions=len(p0_normed), \
                options=options, bounds=pso_bounds, bh_strategy='reflective', vh_strategy='invert', \
                init_pos=init_pos)


            # Run PSO
            cost, pos = optimizer.optimize(fitResidualsListArguments, iters=self.pso_iterations, \
                n_processes=mp.cpu_count() - 1, fit_input_data=fit_input_data, param_string=param_string, \
                const_original=const, traj=self.traj, mini_norm_handle=mini_norm_handle, \
                mag_weight=self.autofit_mag_weight, lag_weights=self.autofit_lag_weights, \
                lag_weight_ht_change=self.autofit_lag_weight_ht_change, verbose=False)

            print(cost, pos)

            fit_params = pos


            # Plot the cost history
            plot_cost_history(optimizer.cost_history)
            plt.show()

            ### ###



        # Enable the fit button
        self.autoFitButton.setDisabled(False)
        self.autoFitButton.setStyleSheet("background-color: #efebe7")


        # Init a Constants instance with fitted parameters
        const_fit = extractConstantParams(const_original, fit_params, param_string, mini_norm_handle)

        # Assign fitted parameters and run the Simulation
        self.const = const_fit
        self.updateInputBoxes()
        self.runSimulationGUI()

        # Store the simulation results prior to auto fit as the previous simulation results
        self.const_prev = const_original
        self.simulation_results_prev = simulation_results_prefit


    def toggleFragmentation(self, event):

        if self.fragmentationGroup.isChecked():

            print("Fragmentation ENABLED!")

            self.const.fragmentation_on = True

        else:

            print("Fragmentation DISABLED!")

            self.const.fragmentation_on = False


    def newFragmentationFile(self):
        """ Choose the location of a new fragmentation file. """

        # Choose the location of the new file
        fragmentation_file_path = QFileDialog.getSaveFileName(self, "Choose the fragmentation file", \
            os.path.join(self.dir_path, "metsim_fragmentation.txt"), "Fragmentation file (*.txt)")[0]

        # If it was cancelled, end function
        if not fragmentation_file_path:
            self.fragmentationGroup.setChecked(False)
            return None

        # Add a file extension if it was not given
        if len(os.path.basename(fragmentation_file_path).split(".")) == 1:
            fragmentation_file_path += ".txt"

        print("New fragmentation file:", fragmentation_file_path)

        self.fragmentation = FragmentationContainer(self, fragmentation_file_path)
        self.fragmentation.newFragmentationFile()
        

    def loadFragmentationFile(self):
        """ Load the fragmentation file for disk. """

        fragmentation_file_path = QFileDialog.getOpenFileName(self, "Choose the fragmentation file", \
            self.dir_path, "Fragmentation file (*.txt)")[0]


        print("Loading fragmentation file:", fragmentation_file_path)


        # Load the fragmentation data
        self.fragmentation = FragmentationContainer(self, fragmentation_file_path)
        self.fragmentation.loadFragmentationFile()

        print("Loaded fragments:")
        for frag_entry in self.fragmentation.fragmentation_entries:
            print(frag_entry.toString())


    def addFragmentation(self, frag_type):
        """ Add a fragmentation line."""

        self.fragmentation.addFragmentation(frag_type)


    def electronDensityMeasChanged(self):
        """ Handle what happens when the electron density measurements are changed. """

        self.readInputBoxes()
        self.showCurrentResults()


    def saveUpdatedOrbit(self):
        """ Save updated orbit and trajectory to file. """

        # Compute the difference between the model and the measured initial velocity
        # NOTE: The ECI, not the ground-fixed velocity needs to be used, as the meteor model does not
        #   include Earth's rotation!
        v_init_diff = self.const.v_init - self.traj.orbit.v_init

        # Recompute the orbit with an increased initial velocity
        orb = calcOrbit(self.traj.radiant_eci_mini, self.traj.v_init + v_init_diff, self.traj.v_avg \
            + v_init_diff, self.traj.state_vect_mini, self.traj.rbeg_jd)


        print(orb)

        
        # Make a file name for the report
        traj_updated = copy.deepcopy(self.traj)
        traj_updated.orbit = orb
        dir_path, file_name = os.path.split(self.traj_path)
        report_file_name = file_name.replace('trajectory.pickle', '') + 'report_sim.txt'

        # Save the report with updated orbit
        traj_updated.saveReport(dir_path, report_file_name, uncertainties=self.traj.uncertainties, verbose=False)

        # Save the updated pickle file
        updated_pickle_file_name = file_name.replace('trajectory.pickle', 'trajectory_sim.pickle')
        savePickle(traj_updated, dir_path, updated_pickle_file_name)



    def saveFitParameters(self, event, suffix=None):
        """ Save fit parameters to a JSON file. """

        if suffix is None:
            suffix = str("")

        dir_path, file_name = os.path.split(self.traj_path)
        file_name = file_name.replace('trajectory.pickle', '') + "sim_fit{:s}.json".format(suffix)


        # Create a copy of the fit parameters
        const = copy.deepcopy(self.const)


        # Convert the density parameters to a list
        if isinstance(const.dens_co, np.ndarray):
            const.dens_co = const.dens_co.tolist()

        # Remove fragments from entries becuase they can't be saved in JSON
        for frag_entry in const.fragmentation_entries:
            del frag_entry.fragments
            frag_entry.resetOutputParameters()


        file_path = os.path.join(dir_path, file_name)
        with open(file_path, 'w') as f:
            json.dump(const, f, default=lambda o: o.__dict__, indent=4)

        print("Saved fit parameters to:", file_path)




    def saveVideo(self, event):
        """ Generate video frames using the simulated wake and PSF. """


        # Skip saving video if there is no wake results
        if self.simulation_results.wake_results is None:
            return False


        # Disable the video button
        self.wakeSaveVideoButton.setStyleSheet("background-color: red")
        self.wakeSaveVideoButton.setDisabled(True)
        self.repaint()


        # The plate scale is fixed at 0.5 meters per pixel at 100 km, so the animation is better visible
        plate_scale = 0.5

        # Frame nackground intensity
        background_intensity = 30


        # Create meshgrid for Gaussian evaluation
        grid_size = 20
        x = np.linspace(-grid_size//2, grid_size//2, grid_size)
        y = np.linspace(-grid_size//2, grid_size//2, grid_size)
        X, Y = np.meshgrid(x, y)
        mesh_list = np.c_[X.flatten(), Y.flatten()]


        # Compute the video size from the pixel scale and wake extent and make sure it's an even number
        frame_ht = frame_wid = int(np.ceil(self.const.wake_extension/plate_scale) + grid_size)
        if frame_ht%2 == 1:
            frame_ht = frame_wid = frame_ht + 1


        # Init the Gaussian
        gauss = scipy.stats.multivariate_normal([0.0, 0.0], [[self.const.wake_psf/np.sqrt(2)/plate_scale, 0 ], 
                                                 [        0,     self.const.wake_psf/np.sqrt(2)/plate_scale]])


        # Get the directory path
        dir_path, _ = os.path.split(self.traj_path)



        # Find the wake index starting at 5 km above the beginning height
        video_beg_ht = self.traj.rbeg_ele + 5000
        wake_beg_indx =  np.argmin(np.abs(video_beg_ht - self.simulation_results.brightest_height_arr))

        # Find the wake index ending at the end height
        video_end_ht = self.traj.rend_ele
        wake_end_indx =  np.argmin(np.abs(video_end_ht - self.simulation_results.brightest_height_arr))

        # Go through all wake points
        for i, (wake, ht) in enumerate(zip(self.simulation_results.wake_results[wake_beg_indx:wake_end_indx],\
            self.simulation_results.brightest_height_arr[wake_beg_indx:wake_end_indx])):

            if wake is None:
                continue

            print('Height: {:.3f} km'.format(ht/1000))


            # Init a new video frame
            frame = np.zeros((frame_ht, frame_wid), dtype=np.float64)


            # Compute pixel scale from length
            pixel_length = wake.length_points/plate_scale


            # Normalize the luminosity by the maximum luminosity (oversaturate the peak so the fainter parts
            #   are better visible)
            luminosities = 2*255*wake.luminosity_points/self.simulation_results.wake_max_lum

            
            # Evaluate the gaussian of every fragment
            for px, lum in zip(pixel_length, luminosities):

                # Compute the gaussian centre
                center = int(px) + frame_wid//2

                # Skip those fragments exiting the FOV
                if center < -grid_size//2:
                    continue

                # Evaluate the gaussian, normalize so that the brightest peak of the meteor is saturating
                gauss_eval = self.const.wake_psf/plate_scale*lum*gauss.pdf(mesh_list).reshape(grid_size, \
                    grid_size)


                ### Add the evaluated gaussian to the frame ###

                # Compute range of pixel coordinates where to add the evaluated window
                x_min = center - grid_size//2
                x_min_eval = 0
                if x_min < 0: 
                    x_min_eval = -x_min
                    x_min = 0

                x_max = center + grid_size//2
                x_max_eval = grid_size
                if x_max >= frame_wid: 
                    x_max_eval = grid_size + frame_wid - x_max
                    x_max = frame_wid - 1

                y_min = center - grid_size//2
                y_min_eval = 0
                if y_min < 0: 
                    y_min_eval = -y_min
                    y_min = 0


                y_max = center + grid_size//2
                y_max_eval = grid_size
                if y_max >= frame_wid: 
                    y_max_eval = grid_size + frame_ht - y_max
                    y_max = frame_ht - 1

                ### ###

                # print()
                # print(center)
                # print(y_min,y_max, x_min,x_max)
                # print(y_min_eval,y_max_eval, x_min_eval,x_max_eval)


                # Add the evaluated gaussian to the frame
                frame[y_min:y_max, x_min:x_max] += gauss_eval[y_min_eval:y_max_eval, x_min_eval:x_max_eval]

            # Add frame background intensity
            frame += background_intensity

            # Save the image to disk
            saveImage(os.path.join(dir_path, "{:04d}_{:7.3f}km.png".format(i, ht/1000)), frame, cmap='gray',
                vmin=0, vmax=255)


        print("Video frame saving done!")
            

        # Enable the video button
        self.wakeSaveVideoButton.setDisabled(False)
        self.wakeSaveVideoButton.setStyleSheet("background-color: #efebe7")







if __name__ == "__main__":

    ### COMMAND LINE ARGUMENTS

    # Init the command line arguments parser
    arg_parser = argparse.ArgumentParser(description="Run meteor ablation modelling using the given trajectory file.")

    arg_parser.add_argument('traj_pickle', metavar='TRAJ_PICKLE', type=str, \
        help="Either the .pickle file with the trajectory solution and the magnitudes, or the path to the folder when the --all option is given.")

    arg_parser.add_argument('-l', '--load', metavar='LOAD_JSON', \
        help="Load JSON file with fit parameters. Instead of giving the full path to the file, you can call it as '--load .' and it will automatically find the file if it exists.", type=str)

    arg_parser.add_argument('-m', '--met', metavar='MET_FILE', \
        help='Load additional observations from a METAL or mirfit .met file.', type=str)

    arg_parser.add_argument('-c', '--lc', metavar='LIGHT_CURVE', \
        help="""Additional light curve given in a comma-separated CSV file. The beginning of every station \
        entry should start with 'Station: Name', followed by the height in km and the absolute magnitude. E.g.
        # Station: XX0001
        # Height (km), Abs Magnitude
          102.3996248,  -8.021
          101.2172498,  -8.036
          100.0348748,  -8.142
           98.8524998,  -8.607
           97.6701248,  -8.703
           96.4877498,  -8.788
           95.3053748,  -9.084
           94.1229998,  -9.399
           92.9406248   -9.475
           91.7582498,  -9.671
           90.5758747,  -9.956
           89.3934997, -10.061
           88.2111247, -10.227
           87.0287497, -10.412
           85.8463747, -10.588
           84.6639997, -10.763
        """, type=str)

    arg_parser.add_argument('-w', '--wid', metavar='WID_FILES', \
        help='Load mirfit wid files which will be used for wake plotting. Wildchars can be used, e.g. /path/wid*.txt.', 
        type=str, nargs='+')

    arg_parser.add_argument('-a', '--all', \
        help="""Automatically find and load the trajectory pickle file, the metal met file, and the wid \
        files. Instead of the path to the trajectory pickle file, a path to folders with data files should \
        be given. For example, if the data is in /home/user/data/20200526_012345_mir and \
        /home/user/data/20200526_012345_met, you should call this module as as: \
        python -m wmpl.MetSim.GUI /home/user/data/20200526_012345 --all --load . \
        """, \
        action="store_true")

    # Parse the command line arguments
    cml_args = arg_parser.parse_args()

    #########################

    # Init PyQt5 window
    app = QApplication([])


    # Automatically find all input files if the --all option is given
    traj_pickle_file = None
    met_file = None
    wid_files = None
    if cml_args.all:

        # Find all folders that match the given regex
        for dir_path in glob.glob(os.path.abspath(cml_args.traj_pickle.rstrip(os.sep) + "*")):

            # Only take directories
            if not os.path.isdir(dir_path):
                continue

            # Try finding the needed files
            for file_name in sorted(os.listdir(dir_path)):

                file_path = os.path.join(dir_path, file_name)

                # If the folder is the metal folder, don't load the trajectory pickle nor wake files
                if not os.path.basename(dir_path).endswith("_met"):

                    # If it's a trajectory pickle file, assign it
                    if traj_pickle_file is None:
                        if file_name.endswith("_trajectory.pickle"):
                            traj_pickle_file = file_path
                            continue

                    # Look for wid files
                    if wid_files is None:
                        if file_name.startswith("wid_") and file_name.endswith(".txt"):
                            wid_files = sorted(glob.glob(os.path.join(dir_path, "wid_*.txt")))
                            continue


                # Look for state files with magnitude measurements
                # Note that there is no check if the met_file has already been found, which means that it
                #   will keep loading state files and take the last one in the end (i.e. the latest one)
                if file_name.startswith("state") and file_name.endswith(".met"):

                    # Try loading the state file and check if it's the METAL state file
                    met = loadMet(dir_path, file_name)

                    # Take the file if it's a METAL state file
                    if not met.mirfit:
                        met_file = file_path


        # Print notices if certain files were not found
        if traj_pickle_file is None:
            print("No trajectory .pickle files found in the given path: {:s}".format(cml_args.traj_pickle))
            sys.exit()

        if met_file is None:
            print("No .met files found in {:s}".format(cml_args.traj_pickle))

        if wid_files is None:
            print("No wid files found in {:s}".format(cml_args.traj_pickle))


    else:

        # Assign individual files
        traj_pickle_file = os.path.abspath(cml_args.traj_pickle)
        met_file = cml_args.met
        wid_files = cml_args.wid



    # Handle auto loading fit parameters
    load_file = None
    if cml_args.load == '.':

        # Extract the directory (or more) where the JSON file with fit parametrs could be
        if os.path.isfile(cml_args.traj_pickle):
            dir_path = os.path.abspath(os.path.dirname(cml_args.traj_pickle))

        else:
            dir_path = os.path.abspath(cml_args.traj_pickle.rstrip(os.sep) + "*")


        # Find the JSON file with the fit parameters
        for dir_path in glob.glob(dir_path):

            # Skip files
            if not os.path.isdir(dir_path):
                continue

            # Find the file with fit parametres (but not the latest file, just the saved parameters)
            for file_name in sorted(os.listdir(dir_path)):

                if file_name.endswith("sim_fit.json"):
                    load_file = os.path.join(dir_path, file_name)
                    break

    else:
        load_file = cml_args.load


    print("Loading trajectory pickle file:")
    print(traj_pickle_file)

    if met_file is not None:
        print("Loading additional magnitudes from .met file:")
        print(met_file)

    if cml_args.lc is not None:
        print("Loading additional light curve from a light curve CSV file:")
        print(cml_args.lc)

    if wid_files is not None:
        print("Loading wake data from:")
        for wfile in wid_files:
            print(wfile)


    if load_file is not None:
        print("Loading fit parameters from:")
        print(load_file)


    # Init the MetSimGUI application
    main_window = MetSimGUI(traj_pickle_file, const_json_file=load_file, \
        met_path=met_file, lc_path=cml_args.lc, wid_files=wid_files)


    main_window.show()

    sys.exit(app.exec_())
