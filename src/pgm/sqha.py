"""
A module serve as sqha, read standard qha input 
"""

from qha.readers import read_input
from qha.fitting import polynomial_least_square_fitting
from qha.grid_interpolation import calculate_eulerian_strain, from_eulerian_strain
from qha.unit_conversion import *
import matplotlib.pyplot as plt
import numpy as np
from scipy.constants import physical_constants as pc
from scipy.constants import electron_volt, angstrom, Avogadro
from numba import vectorize, float64
from typing import Callable, Optional
from scipy.interpolate import UnivariateSpline, InterpolatedUnivariateSpline
from scipy.integrate import cumtrapz
import pandas as pd
import gc
import palettable
from qha.type_aliases import Matrix, Scalar, Vector
from pgm.data import generate_adiabatic_tp
from pgm.thermo import ThermodynamicProperties

HBAR = 100 / pc['electron volt-inverse meter relationship'][0] / pc['Rydberg constant times hc in eV'][0]
K = pc['Boltzmann constant in eV/K'][0] / pc['Rydberg constant times hc in eV'][0]

def spline_interpolation(raw_volumes, raw_energies, discrete_temperatures, continuous_temperatures, calibrate_option = 'first'):
    configurations_amount, volume_number= raw_volumes.shape
    calibrate_option_dict = {'first': 0,
    'f1': 1,
    'mid': round(configurations_amount/2),
    'mid+1': round(configurations_amount/2)+1,
    'mid-1': round(configurations_amount/2)-1,
    'end': -1,
    'l2': -2,
    'l3':-3,
    'f2':2,
    'f3':3}
    index = calibrate_option_dict[calibrate_option]
    interpolated_volumes = np.tile(raw_volumes[index],(len(continuous_temperatures),1))
    calibrated_energy = calibrate_energy_on_reference(raw_volumes, raw_energies, order = 4, calibrate_index = index).T
    interpolated_free_energies = []
    for i in range(volume_number):
        interpolated_free_energies.append(

            InterpolatedUnivariateSpline(
                discrete_temperatures, calibrated_energy[i])(continuous_temperatures)
                )
    # InterpolatedUnivariateSpline < > UnivariateSpline

    interpolated_free_energies = np.array(interpolated_free_energies)

    return interpolated_free_energies.T, interpolated_volumes

def calibrate_energy_on_reference(volumes_before_calibration: Matrix, energies_before_calibration: Matrix,
                                  order: Optional[int] = 3, calibrate_index = 0):
    """
    In multi-configuration system calculation, volume set of each calculation may varies a little,
    This function would make the volume set  of configuration 1 (normally, the most populated configuration)
    as a reference volume set, then calibrate the energies of all configurations to this reference volume set.

    :param volumes_before_calibration: Original volume sets of all configurations
    :param energies_before_calibration: Free energies of all configurations on the corresponding volume sets.
    :param order: The order of Birch--Murnaghan EOS fitting.
    :param calibrate_option: The option to control the calibrate reference
    :return: Free energies of each configuration on referenced volumes (usually the volumes of the first configuration).
    """
    configurations_amount, _ = volumes_before_calibration.shape
    volumes_for_reference: Vector = volumes_before_calibration[calibrate_index]
    energies_after_calibration = np.empty(volumes_before_calibration.shape)
    for i in range(configurations_amount):
        strains_before_calibration = calculate_eulerian_strain(volumes_before_calibration[i, calibrate_index],
                                                          volumes_before_calibration[i])
        strains_after_calibration = calculate_eulerian_strain(volumes_before_calibration[i, calibrate_index], volumes_for_reference)
        _, energies_after_calibration[i, :] = polynomial_least_square_fitting(strains_before_calibration,
                                                                              energies_before_calibration[i],
                                                                              strains_after_calibration,
                                                                              order=order)
    return energies_after_calibration

def entropy(temperature, frequency, weights):
    def vib_entropy(temperature, frequency):
        # frequency = frequency * 1.3
        kt = K * temperature
        mat = np.zeros(frequency.shape)
        for i in range(frequency.shape[0]):
            for j in range(frequency.shape[1]):
                for k in range(frequency.shape[2]):
                    if frequency[i][j][k] <= 0:
                        mat[i][j][k] = 0
                    else:
                        freq = frequency[i][j][k]
                        hw = HBAR * freq
                        hw_2kt = hw / (2*kt)
                        mat[i][j][k] = K * (hw_2kt/np.tanh(hw_2kt)-np.log(2*np.sinh(hw_2kt)))
        return mat

    scaled_q_weights: Vector = weights / np.sum(weights)
    vibrational_entropies: Vector = np.dot(vib_entropy(temperature, frequency).sum(axis=2), scaled_q_weights)
    
    return vibrational_entropies

def zero_point_motion(temperature, frequency, weights):
    def vib_entropy(temperature, frequency):
        kt = K * temperature
        mat = np.zeros(frequency.shape)
        for i in range(frequency.shape[0]):
            for j in range(frequency.shape[1]):
                for k in range(frequency.shape[2]):
                    if frequency[i][j][k] <= 0:
                        mat[i][j][k] = 0
                    else:
                        freq = frequency[i][j][k]
                        hw_2 = HBAR * freq/2
                        mat[i][j][k] = hw_2
        return mat

    scaled_q_weights: Vector = weights / np.sum(weights)
    zero_free_energies: Vector = np.dot(vib_entropy(temperature, frequency).sum(axis=2), scaled_q_weights)
    return zero_free_energies

# def save_data(quantities, index, column, filename):
#     df = pd.DataFrame(quantities, index = index, columns = column)
#     df.to_csv(filename)
#     gc.collect()

def intergrate(temperatures, entropies):
    all_energies = []
    for i, entropy in enumerate(entropies.T):# for same temperature
        energy = cumtrapz(entropy, temperatures, initial=entropy[0])
        all_energies.append(energy)
    
    return - np.array(all_energies).T

class interpolation:
    def __init__(self, in_volumes, num, ratio):
        self.in_volumes = np.array(in_volumes)
        self.num = num
        self.ratio = ratio
        self.out_volumes, self.out_strains, self.in_strains = self.interpolate_volumes
    
    @property
    def interpolate_volumes(self):
        """
        for a vector of volumes, interpolate num, expand the volume by ratio
        """
        in_strains = calculate_eulerian_strain(self.in_volumes[0], self.in_volumes)
        v_min, v_max = np.min(self.in_volumes), np.max(self.in_volumes)
        # r = v_upper / v_max = v_min / v_lower
        v_lower, v_upper = v_min / self.ratio, v_max * self.ratio
        # The *v_max* is a reference value here.
        s_upper, s_lower = calculate_eulerian_strain(v_max, v_lower), calculate_eulerian_strain(v_max, v_upper)
        out_strains = np.linspace(s_lower, s_upper, self.num)
        out_volumes = from_eulerian_strain(v_max, out_strains)
        return out_volumes, out_strains, in_strains
    
    def fitting(self, entropy, order = 3):
        a, new_entropy =  polynomial_least_square_fitting(self.in_strains, entropy, self.out_strains, order)
        return new_entropy

def sqha_whole_pack(temperature,
                    pressure,
                    discrete_temperatures, 
                    folder='data/upto_600GPa/%sK/input.txt', 
                    init_p = 55, 
                    init_t = 1717, 
                    ratio = 1.2
                    ):
    """
    params:
    temperature: a fine temperature grid
    pressure: desired pressure range
    discreate_temperature: different pressure files
    folder: target folders
    init_p: the initial pressure in geothermpy
    init_t: the initial temperature in geothermpy 
    ratio: the volume expansion ratio
    return:
    v: volume
    p: generated geotherm p
    t: generated geotherm t
    interpolation: an integrated thermodynamics property instance
    """
    
    vib_energies, _ = sqha(len(pressure), ratio, discrete_temperatures, temperature, folder)
    volumes, energies, static_energies = calculator(len(pressure), ratio, discrete_temperatures, folder)
    static_free_energies, interpolated_volumes = spline_interpolation(volumes, static_energies, discrete_temperatures, temperature)
    total_free_energies = vib_energies +static_free_energies
    interpolation = Thermodynamics_properties(interpolated_volumes[0], temperature, gpa_to_ry_b3(pressure), total_free_energies)
    gg = interpolation.geotherm
    p, t = generate_adiabatic_tp(gg, temperature, pressure, init_p=init_p, init_t = init_t)
    v = interpolation.get_adiabatic_eos(t, p)
    v = v / 2
    return v, p, t, interpolation

def sqha(NTV, ratio, discrete_temperatures, continuous_temperatures, path = 'data/vibration/%sK/input.txt', calibrate_option = 'first'):
    """
    compute free energies by intergrating entropies
    return:
    vib_free_energies: a continous grid of vibrational free energies
    interpolated_entropies: a continous grid of entropies
    """
    all_volumes = []
    all_entropies = []
    for temp in discrete_temperatures:
        rs = read_input(path % temp)
        inter = interpolation(rs[1], NTV, ratio)
        if temp == 0:
            new_s = np.zeros(NTV)
            # s = entropy(0.01, rs[3], rs[4])
            # new_s = inter.fitting(s)
            # print(s)
        else:
            s = entropy(temp, rs[3], rs[4])
            new_s = inter.fitting(s)

        # s = entropy(temp, rs[3], rs[4])
        # new_s = inter.fitting(s)

        all_volumes.append(inter.out_volumes)
        all_entropies.append(new_s)
    all_volumes = np.array(all_volumes)
    all_entropies = np.array(all_entropies)
    interpolated_entropies, interpolated_volumes = spline_interpolation(all_volumes, all_entropies, discrete_temperatures, continuous_temperatures, calibrate_option= calibrate_option)
    vib_free_energies = intergrate(continuous_temperatures, interpolated_entropies)
    return vib_free_energies, interpolated_entropies

def zero_free_energies(NTV, ratio, discrete_temperatures, continuous_temperatures, path = 'data/vibration/%sK/input.txt'):
    all_volumes = []
    all_energies = []
    for temp in discrete_temperatures:
        rs = read_input(path % temp)
        f = zero_point_motion(temp, rs[3], rs[4])
        inter = interpolation(rs[1], NTV, ratio)
        new_f = inter.fitting(f)
        all_volumes.append(inter.out_volumes)
        all_energies.append(new_f)
    all_volumes = np.array(all_volumes)
    all_energies = np.array(all_energies)
    interpolated_energies, interpolated_volumes = spline_interpolation(all_volumes, all_energies, discrete_temperatures, continuous_temperatures)
    return interpolated_energies


def energy(temperature, frequency, weights):
    """
    Compute vibrational free energies using qha formula
    """
    def vib_energy(temperature, frequency):
        kt = K * temperature
        mat = np.zeros(frequency.shape)
        for i in range(frequency.shape[0]):
            for j in range(frequency.shape[1]):
                for k in range(frequency.shape[2]):
                    if frequency[i][j][k] <= 0:
                        mat[i][j][k] = 0
                    else:
                        freq = frequency[i][j][k]
                        hw = HBAR * freq
                        mat[i][j][k] = 1 / 2 * hw + kt * np.log(1 - np.exp(-hw / kt))
        return mat

    scaled_q_weights: Vector = weights / np.sum(weights)
    vibrational_energies: Vector = np.dot(vib_energy(temperature, frequency).sum(axis=2), scaled_q_weights)
    
    return vibrational_energies
    
def calculator(NTV, ratio, discrete_temperatures, folder = 'data/vibration/%sK/input.txt'):
    """
    Just like the calculator in qha code, it computes everything
    return:
    all_volumes: interpolated volumes at different discreate temperatures
    all_energies: interpolated qha energies at different discreate temperatures
    all_static_energies: interpolated static energies at different discreate temperatures
    """
    all_volumes = []
    all_energies = []
    all_static_energies = []
    for temp in discrete_temperatures:
        rs = read_input(folder % temp)
        vib_f = energy(temp, rs[3], rs[4]) # compute vibrational free energies using qha formula, only vibrational!
        inter = interpolation(rs[1], NTV, ratio)
        static_f = inter.fitting(rs[2])
        new_vib_f = inter.fitting(vib_f)

        all_volumes.append(inter.out_volumes)
        all_energies.append(new_vib_f)
        all_static_energies.append(static_f)

    all_volumes = np.array(all_volumes)
    all_energies = np.array(all_energies)
    all_static_energies = np.array(all_static_energies)
    return all_volumes, all_energies, all_static_energies

def sqha_pure_vib(temperature, pressure, discrete_temperatures, folder='data/upto_600GPa/%sK/input.txt', extra=False, init_p = 55, init_t = 1717, ratio = 1.2):
    vib_energies, _ = sqha(len(pressure), ratio, discrete_temperatures, temperature, folder)
    volumes, energies, static_energies = calculator(len(pressure), ratio, discrete_temperatures, folder)
    static_free_energies, _ = spline_interpolation(volumes, static_energies, discrete_temperatures, temperature)
    total_free_energies = vib_energies.T# +static_free_energies
    interpolation = Thermodynamics_properties(volumes[0], temperature, gpa_to_ry_b3(pressure), total_free_energies.T)
    return interpolation


if __name__ == '__main__':
    pass
