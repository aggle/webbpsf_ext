from pathlib import Path
import numpy as np
import matplotlib
import matplotlib.pyplot as plt

import os, re

from astropy.io import fits, ascii
from astropy.table import Table
import astropy.units as u

from scipy.interpolate import griddata, RegularGridInterpolator, interp1d

from . import conf
from . import synphot_ext as s_ext
from .bandpasses import miri_filter, nircam_filter
from .robust import medabsdev

import logging
_log = logging.getLogger('webbpsf_ext')

from . import __path__
_spec_dir = Path(__path__[0]) / 'spectral_data/'

def BOSZ_filename(Teff, metallicity, log_g, res, carbon=0, alpha=0):
    """ Generate filename for BOSZ spectrum. """

    teff_str = f't{Teff:04.0f}'
    logg_str = 'g{:02.0f}'.format(int(log_g*10))

    # Metallicity [M/H]
    metal_str = 'mp{:02.0f}'.format(int(abs(metallicity*10)+0.5))
    if metallicity<0:
        metal_str = metal_str.replace('p', 'm')

    # Carbon abundance [C/M]
    carb_str = 'cp{:02.0f}'.format(int(abs(carbon*10)+0.5))
    if carbon<0:
        carb_str = carb_str.replace('p', 'm')

    # alpha-element value [alpha/H]
    alpha_str = 'op{:02.0f}'.format(int(abs(alpha*10)+0.5))
    if alpha<0:
        alpha_str = alpha_str.replace('p', 'm')

    # Resolution
    rstr = 'b{}'.format(res)

    # Final file name
    fname = f'a{metal_str}{carb_str}{alpha_str}{teff_str}{logg_str}v20modrt0{rstr}rs.fits'

    return fname

def download_BOSZ_spectrum(Teff, metallicity, log_g, res, carbon=0, alpha=0,
                           outdir=None):

    import requests

    if outdir is None:
        res_dir = os.path.join(_spec_dir, 'bosz_grids', 'R{}'.format(res))

        # Create resolution directory if it doesn't exists
        if not os.path.isdir(res_dir):
            os.makedirs(res_dir)
        outdir = res_dir

    # Generate URL directory that file is saved in
    url_base = 'http://archive.stsci.edu/missions/hlsp/bosz/fits/'
    res_str = f'insbroad_{res:06.0f}'
    metal_str = f'metal_{metallicity:+.2f}'
    carbon_str = f'carbon_{carbon:+.2f}'
    alpha_str = f'alpha_{alpha:+.2f}'
    url_dir = os.path.join(url_base,res_str,metal_str,carbon_str,alpha_str)

    # Generate file name
    fname = BOSZ_filename(Teff, metallicity, log_g, res, carbon=carbon, alpha=alpha)

    # Final URL
    url_final = os.path.join(url_dir, fname)

    # Make request
    _log.info(f'Downloading file: {fname}')
    req = requests.get(url_final, allow_redirects=True)

    # Raise exception if file not found or other HTTP error
    if req.status_code != requests.codes.ok:
        req.raise_for_status()

    # Save file to directory
    outpath = os.path.join(outdir, fname)
    _log.info(f'Saving file to: {outpath}')
    open(outpath, 'wb').write(req.content)


def BOSZ_spectrum(Teff, metallicity, log_g, res=2000, interpolate=True, 
    carbon=0, alpha=0, fluxout='photlam', **kwargs):
    """BOSZ stellar atmospheres (Bohlin et al 2017).

    Read in a spectrum from the BOSZ stellar atmosphere models database.
    Returns a synphot spectral object. Wavelength values range between
    1000 Angstroms to 32 microns. Teff range from 3500K to 36000K.

    This function interpolates the model grid by reading in those models
    closest in temperature, metallicity, and log g to the desired parameters,
    then takes the weighted average of these models based on their relative
    offsets. Can also just read in the closest model by setting interpolate=False.

    Different spectral resolutions can also be specified.

    Parameters
    ----------
    Teff : float
        Effective temperature ranging from 3500K to 30000K.
    metallicity : float
        Metallicity [Fe/H] value ranging from -2.5 to 0.5.
    log_g : float
        Surface gravity (log g) from 0 to 5.

    Keyword Args
    ------------
    carbon : float
        Carbon abundance [C/M]. Must be either [-0.75,-0.5,-0.25, 0, 0.25, 0.5].
    alpha : float
        alpha-element value [alpha/H]. Must be either [-0.25, 0, 0.25, 0.5]
    res : str
        Spectral resolution to use (instrument broadening). Valid points:
        [200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 300000]
    interpolate : bool
        Interpolate spectrum using a weighted average of grid points
        surrounding the desired input parameters.


    References
    ----------
    https://archive.stsci.edu/prepds/bosz/
    """

    model_dir = os.path.join(_spec_dir, 'bosz_grids/')
    res_dir = os.path.join(model_dir, f'R{res:.0f}/')

    if not os.path.isdir(_spec_dir):
        raise IOError(f'Spectral directory does not exist: {_spec_dir}')
    if not os.path.isdir(res_dir):
        os.makedirs(res_dir)
        # raise IOError('Resolution directory does not exist: {}'.format(res_dir))

    # Grid of computed temperature steps
    teff_grid = list(range(3500,12000,250)) \
                + list(range(12000,20000,500)) \
                + list(range(20000,36000,1000))
    teff_grid = np.array(teff_grid)

    # Grid of log g steps for desired Teff
    lg_max = 5
    lg_step = 0.5
    if   Teff <=  6000: lg_min = 0
    elif Teff <=  8000: lg_min = 1
    elif Teff <= 12000: lg_min = 2
    elif Teff <= 30000: lg_min = 3
    elif Teff <= 35000: lg_min = 3.5
    else: raise ValueError('Teff must be less than or equal to 35000K.')

    if log_g<lg_min:
        raise ValueError('log_g must be >={}'.format(lg_min))
    if log_g>lg_max:
        raise ValueError('log_g must be <={}'.format(lg_max))

    # Grid of log g values
    logg_grid = np.arange(lg_min, lg_max+lg_step, lg_step)

    # Grid of metallicity values
    metal_grid = np.arange(-2.5,0.75,0.25)

    # First, choose the two grid points closest in Teff
    teff_diff = np.abs(teff_grid - Teff)
    ind_sort = np.argsort(teff_diff)
    if teff_diff[ind_sort[0]]==0: # Exact
        teff_best = np.array([teff_grid[ind_sort[0]]])
    else: # Want to interpolate
        teff_best = teff_grid[ind_sort[0:2]]

    # Choose the two best log g values
    logg_diff = np.abs(logg_grid - log_g)
    ind_sort = np.argsort(logg_diff)
    if logg_diff[ind_sort[0]]==0: # Exact
        logg_best = np.array([logg_grid[ind_sort[0]]])
    else: # Want to interpolate
        logg_best = logg_grid[ind_sort[0:2]]

    # Choose the two best metallicity values
    metal_diff = np.abs(metal_grid - metallicity)
    ind_sort = np.argsort(metal_diff)
    if metal_diff[ind_sort[0]]==0: # Exact
        metal_best = np.array([metal_grid[ind_sort[0]]])
    else: # Want to interpolate
        metal_best = metal_grid[ind_sort[0:2]]

    # Build final file names
    fnames = []
    # Build lists of properties to pass to download function if needed
    teff_all = []
    logg_all = []
    metal_all = []
    for t in teff_best:
        for l in logg_best:
            for m in metal_best:
                fname = BOSZ_filename(t, m, l, res, carbon=carbon, alpha=alpha)
                fnames.append(fname)
                teff_all.append(t)
                logg_all.append(l)
                metal_all.append(m)
    teff_all = np.array(teff_all)
    logg_all = np.array(logg_all)
    metal_all = np.array(metal_all)

    # Weight by relative distance from desired value
    weights = []
    teff_diff = np.abs(teff_best - Teff)
    logg_diff = np.abs(logg_best - log_g)
    metal_diff = np.abs(metal_best - metallicity)
    for t in teff_diff:
        wt = 1 if len(teff_diff)==1 else t / np.sum(teff_diff)
        for l in logg_diff:
            wl = 1 if len(logg_diff)==1 else l / np.sum(logg_diff)
            for m in metal_diff:
                wm = 1 if len(metal_diff)==1 else m / np.sum(metal_diff)
                weights.append(wt*wl*wm)
    weights = np.array(weights)
    weights = weights / np.sum(weights)

    if interpolate:
        wave_all = []
        flux_all = []
        for i, f in enumerate(fnames):
            # Download files that don't currently exist
            fpath = os.path.join(res_dir, f)
            if not os.path.isfile(fpath):
                download_BOSZ_spectrum(teff_all[i], metal_all[i], logg_all[i], res,
                                       carbon=carbon, alpha=alpha)

            d = fits.getdata(fpath, 1)
            wave_all.append(d['Wavelength'])
            flux_all.append(d['SpecificIntensity'] * weights[i])

        wfin = wave_all[0]
        ffin = np.pi * np.array(flux_all).sum(axis=0) # erg/s/cm^2/A
    else:
        ind = np.where(weights==weights.max())[0][0]
        f = fnames[ind]
        Teff = teff_all[ind]
        log_g = logg_all[ind]
        metallicity = metal_all[ind]

        # Download files if doesn't exist
        fpath = os.path.join(res_dir, f)
        if not os.path.isfile(fpath):
            download_BOSZ_spectrum(Teff, metallicity, log_g, res,
                                   carbon=carbon, alpha=alpha)

        d = fits.getdata(fpath, 1)
        wfin = d['Wavelength']
        ffin = np.pi * d['SpecificIntensity'] # erg/s/cm^2/A


    name = 'BOSZ(Teff={},z={},logG={})'.format(Teff, metallicity, log_g)
    sp = s_ext.ArraySpectrum(wave=wfin[:-1], flux=ffin[:-1], 
                             waveunits='angstrom', fluxunits='flam', name=name)

    sp.convert(fluxout)
    return sp

def BOSZ_filename_2024(Teff, log_g, metallicity, res, alpha=0, carbon=0, 
                       micro_vel=2, prod=None, **kwargs):
    """ Generate filename for BOSZ 2024 spectrum
    
    MARCS ("ms" or "mp"):

        * Teff from 2800 to 4000, in steps of 100; log(g) -0.5 to 5.5, in steps of 0.5
        * Teff from 4250 to 4750, in steps of 250; log(g) -0.5 to 5.0, in steps of 0.5
        * Teff from 5000 to 5750, in steps of 250; log(g) 0.0 to 5.5, in steps of 0.5
        * Teff from 6000 to 7000, in steps of 250; log(g) 1.0 to 5.5, in steps of 0.5
        * Teff from 7250 to 8000, in steps of 250; log(g) 2.0 to 5.5, in steps of 0.5

    ATLAS9 ("ap"):

        * Teff from 7500 to 12000, in steps of 250; log(g) 2.0 to 5.0, in steps of 0.5
        * Teff from 12500 to 16000, in steps of 500; log(g) 3.0 to 5.0, in steps of 0.5

    Parameters
    ----------
    Teff : float
        Effective temperature ranging from 2800K to 16000K.
    log_g : float
        Surface gravity (log g) from -0.5 to 5.5.
    metallicity : float
        Metallicity [Fe/H] value ranging from -2.50 to +0.75, 
        in steps of 0.25
    res : str
        Spectral resolution: 500, 1000, 2000, 5000, 10000, 20000, 50000, or 'orig'. 

    Keyword Args
    ------------
    alpha : float
        alpha-element value [alpha/M]. Must be either [-0.25, 0, 0.25, 0.5]
    carbon : float
        Carbon abundance [C/M]. Must be either [-0.75,-0.5,-0.25, 0, 0.25, 0.5].
    micro_vel : float
        microturbulence velocity, either 0, 1, 2, or 4 km/s
    prod : str
        Product type is either None, 'wave', or 'lineid'. 
        If None, the default is either 'resam' or 'noresamp' if res='orig'. 
        If 'wave', the product is the wavelength file 'bosz2024_wave_r{res}.txt'.
        If 'lineid', the product is the line identification file, only valid
        for res='orig'.
    """

    # Effective Temperature
    teff_str = f't{Teff:04.0f}'

    # Surface gravity
    logg_str = f'g{log_g:+02.1f}'

    # In the MARCS model atmospheres, spherical geometry is used between 
    # logg=−1 and 3 with vmicro=2 kms−1; and plane-parallel geometry is 
    # assumed between logg= 3.5 and 5.5 dex with vmicro=1 kms−1.
    # All ATLAS9 models are plane-parallel model atmospheres with vmicro=2 kms−1.
    is_marcs = Teff <= 8000
    if is_marcs:
        atmo_str = 'ms' if log_g < 3.5 else 'mp'
    else:
        atmo_str = 'ap'

    # Metallicity [M/H]
    metal_str = f'm{metallicity:+03.2f}'

    # alpha-element value [alpha/M]
    alpha_str = f'a{alpha:+03.2f}'

    # Carbon abundance [C/M]
    carb_str = f'c{carbon:+03.2f}'

    # Microturbulence
    micro_str = f'v{micro_vel:01.0f}'

    # Resolution
    # res can also equal 'orig' for the original resolution
    rstr = f'r{res}'

    # Product type is either 'resam' or 'noresam'
    if prod is None:
        prod_str = 'noresam' if res == 'orig' else 'resam'
    elif prod=='lineid' and res=='orig':
        prod_str = 'lineid'
    elif prod=='lineid':
        raise ValueError('Product type "lineid" is only valid for res="orig".')
    elif (prod == 'wave') and (res != 'orig'):
        return f'bosz2024_wave_r{res}.txt'
    elif (prod == 'wave'):
        raise ValueError('Product type "wave" is not valid for res="orig".')
    else:
        raise ValueError(f'Invalid product type: {prod}')

    # Final file name
    # bosz2024_mp_t5000_g+5.0_m+0.00_a+0.00_c+0.00_v0_r500_resam.txt.gz
    # bosz2024_<atmos>_<teff>_<logg>_<metal>_<alpha>_<carbon>_<micro>_<insbroad>_<prod>.txt.gz
    fname = f'bosz2024_{atmo_str}_{teff_str}_{logg_str}_{metal_str}_{alpha_str}_{carb_str}_{micro_str}_{rstr}_{prod_str}.txt.gz'

    return fname

def download_BOSZ_2024(Teff, log_g, metallicity, res, outdir=None, **kwargs):

    import requests

    if outdir is None:
        res_dir = os.path.join(_spec_dir, 'bosz2024_grids', 'R{}'.format(res))

        # Create resolution directory if it doesn't exists
        if not os.path.isdir(res_dir):
            os.makedirs(res_dir)
        outdir = res_dir

    # Check for product keyword of type is

    is_wave = kwargs.get('prod', 'none') == 'wave'
    
    # Generate URL directory that file is saved in
    url_base = 'https://archive.stsci.edu/hlsps/bosz/bosz2024/'
    res_str = f'r{res}'
    metal_str = f'm{metallicity:+03.2f}'
    if is_wave:
        url_dir = os.path.join(url_base, 'wavelength_grids')
    else:
        url_dir = os.path.join(url_base, res_str, metal_str)

    # Generate file name
    fname = BOSZ_filename_2024(Teff, log_g, metallicity, res, **kwargs)

    # Final URL
    url_final = os.path.join(url_dir, fname)

    # Make request
    _log.info(f'Downloading file: {fname}')
    req = requests.get(url_final, allow_redirects=True)

    # Raise exception if file not found or other HTTP error
    if req.status_code != requests.codes.ok:
        req.raise_for_status()

    # Save file to directory
    outpath = os.path.join(outdir, fname)
    _log.info(f'Saving file to: {outpath}')
    open(outpath, 'wb').write(req.content)


def BOSZ_2024_spectrum(Teff, log_g, metallicity, res=2000, 
                       interpolate=True, fluxout='photlam', **kwargs):
    """BOSZ stellar atmospheres (Mészáros et al. 2024).

    Read in a spectrum from the BOSZ stellar atmosphere models database.
    Returns a synphot spectral object. Wavelength values range between
    1000 Angstroms to 32 microns. Teff range from 3500K to 36000K.

    This function interpolates the model grid by reading in those models
    closest in temperature, metallicity, and log g to the desired parameters,
    then takes the weighted average of these models based on their relative
    offsets. Can also just read in the closest model by setting interpolate=False.

    Different spectral resolutions can also be specified.

    Parameters
    ----------
    Teff : float
        Effective temperature ranging from 3500K to 30000K.
    metallicity : float
        Metallicity [Fe/H] value ranging from -2.5 to 0.5.
    log_g : float
        Surface gravity (log g) from 0 to 5.

    Keyword Args
    ------------
    carbon : float
        Carbon abundance [C/M]. Must be either [-0.75,-0.5,-0.25, 0, 0.25, 0.5].
    alpha : float
        alpha-element value [alpha/H]. Must be either [-0.25, 0, 0.25, 0.5]
    res : str
        Spectral resolution to use (instrument broadening). Valid points:
        [200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 300000]
    interpolate : bool
        Interpolate spectrum using a weighted average of grid points
        surrounding the desired input parameters.


    References
    ----------
    https://archive.stsci.edu/prepds/bosz/
    """

    model_dir = os.path.join(_spec_dir, 'bosz2024_grids/')
    res_dir = os.path.join(model_dir, f'R{res:.0f}/')

    if not os.path.isdir(_spec_dir):
        raise IOError(f'Spectral directory does not exist: {_spec_dir}')
    if not os.path.isdir(res_dir):
        os.makedirs(res_dir)
        # raise IOError('Resolution directory does not exist: {}'.format(res_dir))

    # Grid of computed temperature steps
    teff_grid = list(range(2800,4000,100)) \
                + list(range(4000,12000,250)) \
                + list(range(12000,16500,500))
    teff_grid = np.array(teff_grid)

    # Grid of log g steps for desired Teff
    lg_step = 0.5
    if   Teff <=  4000: lg_min, lg_max = (-0.5, 5.5)
    elif Teff <=  4650: lg_min, lg_max = (-0.5, 5.0)
    elif Teff <=  5750: lg_min, lg_max = ( 0.0, 5.5)
    elif Teff <=  7000: lg_min, lg_max = ( 1.0, 5.5)
    elif Teff <=  8000: lg_min, lg_max = ( 2.0, 5.5)
    elif Teff <= 12000: lg_min, lg_max = ( 2.0, 5.0)
    elif Teff <= 16000: lg_min, lg_max = ( 3.0, 5.0)    
    else: raise ValueError('Teff must be less than or equal to 16000K.')
    if log_g<lg_min:
        raise ValueError(f'log_g must be >= {lg_min:+.1f}')
    if log_g>lg_max:
        raise ValueError(f'log_g must be <= {lg_max:+.1f}')

    # Grid of log g values
    logg_grid = np.arange(lg_min, lg_max+lg_step, lg_step)

    # Grid of metallicity values
    metal_grid = np.arange(-2.5,0.75,0.25)

    # First, choose the two grid points closest in Teff
    teff_diff = np.abs(teff_grid - Teff)
    ind_sort = np.argsort(teff_diff)
    if teff_diff[ind_sort[0]]==0: # Exact
        teff_best = np.array([teff_grid[ind_sort[0]]])
    else: # Want to interpolate
        teff_best = teff_grid[ind_sort[0:2]]

    # Choose the two best log g values
    logg_diff = np.abs(logg_grid - log_g)
    ind_sort = np.argsort(logg_diff)
    if logg_diff[ind_sort[0]]==0: # Exact
        logg_best = np.array([logg_grid[ind_sort[0]]])
    else: # Want to interpolate
        logg_best = logg_grid[ind_sort[0:2]]

    # Choose the two best metallicity values
    metal_diff = np.abs(metal_grid - metallicity)
    ind_sort = np.argsort(metal_diff)
    if metal_diff[ind_sort[0]]==0: # Exact
        metal_best = np.array([metal_grid[ind_sort[0]]])
    else: # Want to interpolate
        metal_best = metal_grid[ind_sort[0:2]]

    # Get wavelength file
    if res != 'orig':
        fname_wave = BOSZ_filename_2024(Teff, log_g, metallicity, res, prod='wave')
        fpath_wave = os.path.join(res_dir, fname_wave)
        if not os.path.isfile(fpath_wave):
            download_BOSZ_2024(Teff, log_g, metallicity, res, prod='wave', outdir=res_dir)
        tbl_wave = ascii.read(fpath_wave, names=['Wavelength'], format='basic')

        colnames = ['SpecificIntensity', 'Continuum']
    else:
        colnames = ['Wavelength', 'SpecificIntensity', 'Continuum']

    # Build final file names
    fnames = []
    # Build lists of properties to pass to download function if needed
    teff_all = []
    logg_all = []
    metal_all = []
    for t in teff_best:
        for l in logg_best:
            for m in metal_best:
                fname = BOSZ_filename_2024(t, l, m, res, **kwargs)
                fnames.append(fname)
                teff_all.append(t)
                logg_all.append(l)
                metal_all.append(m)
    teff_all = np.array(teff_all)
    logg_all = np.array(logg_all)
    metal_all = np.array(metal_all)

    # Weight by relative distance from desired value
    weights = []
    teff_diff = np.abs(teff_best - Teff)
    logg_diff = np.abs(logg_best - log_g)
    metal_diff = np.abs(metal_best - metallicity)
    for t in teff_diff:
        wt = 1 if len(teff_diff)==1 else t / np.sum(teff_diff)
        for l in logg_diff:
            wl = 1 if len(logg_diff)==1 else l / np.sum(logg_diff)
            for m in metal_diff:
                wm = 1 if len(metal_diff)==1 else m / np.sum(metal_diff)
                weights.append(wt*wl*wm)
    weights = np.array(weights)
    weights = weights / np.sum(weights)

    if interpolate:
        wave_all = []
        flux_all = []
        for i, f in enumerate(fnames):
            # Download files that don't currently exist
            fpath = os.path.join(res_dir, f)
            if not os.path.isfile(fpath):
                download_BOSZ_2024(teff_all[i], logg_all[i], metal_all[i], res, 
                                   outdir=res_dir, **kwargs)

            tbl = ascii.read(fpath, names=colnames, format='basic')
            if res == 'orig':
                wave_all.append(tbl['Wavelength'])
            flux_all.append(tbl['SpecificIntensity'] * weights[i])

        wfin = wave_all[0] if len(wave_all)>0 else tbl_wave['Wavelength']
        ffin = 4 * np.pi * np.array(flux_all).sum(axis=0) # erg/s/cm^2/A
    else:
        ind = np.where(weights==weights.max())[0][0]
        f = fnames[ind]
        Teff = teff_all[ind]
        log_g = logg_all[ind]
        metallicity = metal_all[ind]

        # Download files if doesn't exist
        fpath = os.path.join(res_dir, f)
        if not os.path.isfile(fpath):
            download_BOSZ_2024(Teff, log_g, metallicity, res, outdir=res_dir, **kwargs)
        
        tbl = ascii.read(fpath, names=colnames, format='basic')
        wfin = tbl['Wavelength'] if res == 'orig' else tbl_wave['Wavelength']
        ffin = 4 * np.pi * tbl['SpecificIntensity'] # erg/s/cm^2/A

        # d = fits.getdata(fpath, 1)
        # wfin = d['Wavelength']
        # ffin = np.pi * d['SpecificIntensity'] # erg/s/cm^2/A


    name = 'BOSZ(Teff={},z={},logG={})'.format(Teff, metallicity, log_g)
    sp = s_ext.ArraySpectrum(wave=wfin[:-1], flux=ffin[:-1], 
                             waveunits='angstrom', fluxunits='flam', name=name)

    sp.convert(fluxout)
    return sp


#  TABLE 2: Suggested models for specific stellar types
#      Type    T_{eff}    log_g       Kurucz model
#      O3V      44852     +3.92      ckp00_45000[g45]
#      O5V      40862     +3.92      ckp00_41000[g45]
#      O5.5V    39865     +3.92      ckp00_40000[g40]
#      O6V      38867     +3.92      ckp00_39000[g40]
#      O6.5V    37870     +3.92      ckp00_38000[g40]
#      O7V      36872     +3.92      ckp00_37000[g40]
#      O7.5V    35874     +3.92      ckp00_36000[g40]
#      O8V      34877     +3.92      ckp00_35000[g40]
#      O8.5     33879     +3.92      ckp00_34000[g40]
#      O9V      32882     +3.92      ckp00_33000[g40]
#      O9.5     31884     +3.92      ckp00_32000[g40]
#      B0V      30000     +3.90      ckp00_30000[g40]
#      B1V      25400     +3.90      ckp00_25000[g40]
#      B3V      18700     +3.94      ckp00_19000[g40]
#      B5V      15400     +4.04      ckp00_15000[g40]
#      B8V      11900     +4.04      ckp00_12000[g40]
#      A0V       9520     +4.14       ckp00_9500[g40]
#      A1V       9230     +4.10       ckp00_9250[g40]
#      A3V       8270     +4.20       ckp00_8750[g40]
#      A5V       8200     +4.29       ckp00_8250[g40]
#      F0V       7200     +4.34       ckp00_7250[g40]
#      F2V       6890     +4.34       ckp00_7000[g40]
#      F5V       6440     +4.34       ckp00_6500[g40]
#      F8V       6200     +4.40       ckp00_6250[g40]
#      G0V       6030     +4.39       ckp00_6000[g45]
#      G2V       5860     +4.40       ckp00_5750[g45]
#      G8V       5570     +4.50       ckp00_5500[g45]
#      K0V       5250     +4.49       ckp00_5250[g45]
#      K2V       4780      +4.5       ckp00_4750[g45]
#      K4V       4560      +4.5       ckp00_4500[g45]
#      K5V       4350     +4.54       ckp00_4250[g45]
#      K7V       4060      +4.5       ckp00_4000[g45]
#      M0V       3850     +4.59       ckp00_3750[g45]
#      M2V       3580     +4.64       ckp00_3500[g45]
#      M6V       3050     +5.00       ckp00_3500[g50]
#      B0III    29000     +3.34      ckp00_29000[g35]
#      B5III    15000     +3.49      ckp00_15000[g35]
#      G0III     5850     +2.94       ckp00_5750[g30]
#      G5III     5150     +2.54       ckp00_5250[g25]
#      K0III     4750     +2.14       ckp00_4750[g20]
#      K5III     3950     +1.74       ckp00_4000[g15]
#      M0III     3800     +1.34       ckp00_3750[g15]
#      BOI      26000     +2.84      ckp00_26000[g30]
#      B5I      13600     +2.44      ckp00_14000[g25]
#      AOI       9730     +2.14       ckp00_9750[g20]
#      A5I       8510     +2.04       ckp00_8500[g20]
#      F0I       7700     +1.74       ckp00_7750[g20]
#      F5I       6900     +1.44       ckp00_7000[g15]
#      G0I       5550     +1.34       ckp00_5500[g10]
#      G5I       4850     +1.14       ckp00_4750[g10]
#      K0I       4420     +0.94       ckp00_4500[g10]
#      K5I       3850     +0.00       ckp00_3750[g00]
#      M0I       3650     -0.10       ckp00_3750[g00]
#      M2I       3600     -0.10       ckp00_3500[g00]

def stellar_spectrum(sptype, *renorm_args, **kwargs):
    """Stellar spectrum

    Similar to specFromSpectralType() in STPSF/Poppy, this function uses
    a dictionary of fiducial values to determine an appropriate spectral model.
    If the input spectral type is not found, this function interpolates the
    effective temperature, metallicity, and log g values .

    You can also specify renormalization arguments to pass to ``sp.renorm()``.
    The order (after ``sptype``) should be (``value, units, bandpass``):

    >>> sp = stellar_spectrum('G2V', 10, 'vegamag', bp)

    Flat spectrum (in photlam) are also allowed via the 'flat' string.

    Use ``catname='bosz'`` for BOSZ stellar atmosphere (ATLAS9) (default)
    Use ``catname='ck04models'`` keyword for ck04 models
    Use ``catname='phoenix'`` keyword for Phoenix models

    Keywords exist to directly specify Teff, metallicity, an log_g rather
    than a spectral type.

    Parameters
    ----------
    sptype : str
        Spectral type, such as 'A0V' or 'K2III'.
    renorm_args : tuple
        Renormalization arguments to pass to ``sp.renorm()``.
        The order (after ``sptype``) should be (``value, units, bandpass``)
        Bandpass should be a :class:`s_ext.Bandpass` type.

    Keyword Args
    ------------
    catname : str
        Catalog name, including 'bosz', 'ck04models', and 'phoenix'.
        Default is 'bosz', which comes from :func:`BOSZ_spectrum`.
    Teff : float
        Effective temperature ranging from 2800K to 35000K.
    metallicity : float
        Metallicity [Fe/H] value ranging from -2.5 to 0.5.
    log_g : float
        Surface gravity (log g) from 0 to 5.
    res : str
        BOSZ spectral resolution to use (200 or 2000 or 20000).
        Default: 2000.
    interpolate : bool
        Interpolate BOSZ spectrum using a weighted average of grid points
        surrounding the desired input parameters. 
        Default: True
    """

    def call_bosz(v0, v1, v2, use_2024=True, **kwargs):

        # Use earlier version if Teff > 16000
        if v0 > 16000 and use_2024:
            use_2024=False

        if use_2024:
            if v0 < 2800:
                v0 = 2800
                _log.warning("BOSZ models start at 2800K. Setting Teff=2800.")
            return BOSZ_2024_spectrum(v0, v2, v1, **kwargs)
        else:
            if v0 > 35000:
                v0 = 35000
                _log.warning("BOSZ models stop at 35000K. Setting Teff=35000.")
            if v0 < 3500:
                v0 = 3500
                _log.warning("BOSZ models start at 3500K. Setting Teff=3500.")
            return BOSZ_spectrum(v0, v1, v2, **kwargs)


    Teff = kwargs.pop('Teff', None)
    metallicity = kwargs.pop('metallicity', None)
    log_g = kwargs.pop('log_g', None)

    catname = kwargs.get('catname', 'bosz')
    lookuptable = {
        # https://www.pas.rochester.edu/~emamajek/EEM_dwarf_UBVIJHK_colors_Teff.txt
        "O0V": (50000, 0.0, 4.0),  # Bracketing for interpolation
        "O3V": (46000, 0.0, 4.0),
        "O5V": (43000, 0.0, 4.5),
        "O7V": (36500, 0.0, 4.0),
        "O9V": (32500, 0.0, 4.0),
        "B0V": (31500, 0.0, 4.0),
        "B1V": (26000, 0.0, 4.0),
        "B3V": (17000, 0.0, 4.0),
        "B5V": (15700, 0.0, 4.0),
        "B8V": (12500, 0.0, 4.0),
        "A0V": (9700, 0.0, 4.0),
        "A1V": (9200, 0.0, 4.0),
        "A3V": (8550, 0.0, 4.0),
        "A5V": (8080, 0.0, 4.0),
        "F0V": (7220, 0.0, 4.0),
        "F2V": (6810, 0.0, 4.0),
        "F5V": (6510, 0.0, 4.0),
        "F8V": (6170, 0.0, 4.5),
        "G0V": (5920, 0.0, 4.5),
        "G2V": (5770, 0.0, 4.5),
        "G5V": (5660, 0.0, 4.5),
        "G8V": (5490, 0.0, 4.5),
        "K0V": (5280, 0.0, 4.5),
        "K2V": (5040, 0.0, 4.5),
        "K5V": (4410, 0.0, 4.5),
        "K7V": (4070, 0.0, 4.5),
        "M0V": (3870, 0.0, 4.5),
        "M2V": (3550, 0.0, 4.5),
        "M5V": (3030, 0.0, 5.0),
        "M9V": (2400, 0.0, 5.0),   # Bracketing for interpolation
        "O0IV": (50000, 0.0, 3.8),  # Bracketing for interpolation
        "B0IV": (30000, 0.0, 3.8),
        "B8IV": (12000, 0.0, 3.8),
        "A0IV": (9500, 0.0, 3.8),
        "A5IV": (8250, 0.0, 3.8),
        "F0IV": (7250, 0.0, 3.8),
        "F8IV": (6250, 0.0, 4.3),
        "G0IV": (6000, 0.0, 4.3),
        "G8IV": (5500, 0.0, 4.3),
        "K0IV": (5250, 0.0, 4.3),
        "K7IV": (4000, 0.0, 4.3),
        "M0IV": (3750, 0.0, 4.3),
        "M9IV": (3000, 0.0, 4.7),    # Bracketing for interpolation
        "O0III": (55000, 0.0, 3.5),  # Bracketing for interpolation
        "B0III": (29000, 0.0, 3.5),
        "B5III": (15000, 0.0, 3.5),
        "G0III": (5750, 0.0, 3.0),
        "G5III": (5250, 0.0, 2.5),
        "K0III": (4750, 0.0, 2.0),
        "K5III": (4000, 0.0, 1.5),
        "M0III": (3750, 0.0, 1.5),
        "M6III": (3000, 0.0, 1.0),  # Bracketing for interpolation
        "O0I": (45000, 0.0, 5.0),  # Bracketing for interpolation
        "O6I": (39000, 0.0, 4.5),
        "O8I": (34000, 0.0, 4.0),
        "B0I": (26000, 0.0, 3.0),
        "B5I": (14000, 0.0, 3.0),
        "A0I": (9750, 0.0, 2.0),
        "A5I": (8500, 0.0, 2.0),
        "F0I": (7750, 0.0, 2.0),
        "F5I": (7000, 0.0, 1.5),
        "G0I": (5500, 0.0, 1.5),
        "G5I": (4750, 0.0, 1.0),
        "K0I": (4500, 0.0, 1.0),
        "K5I": (3750, 0.0, 0.5),
        "M0I": (3750, 0.0, 0.0),
        "M2I": (3500, 0.0, 0.0),
        "M5I": (3000, 0.0, 0.0), # Bracketing for interpolation
    }  

    def sort_sptype(typestr):
        letter = typestr[0]
        lettervals = {'O': 0, 'B': 1, 'A': 2, 'F': 3, 'G': 4, 'K': 5, 'M': 6}
        value = lettervals[letter] * 1.0
        value += (int(typestr[1]) * 0.1)
        if "III" in typestr:
            value += 30
        elif "I" in typestr:
            value += 10
        elif "V" in typestr:
            value += 50
        return value

    # Generate list of spectral types
    sptype_list = list(lookuptable.keys())

    # Test if the user wants a flat spectrum (in photlam)
    # Check if Teff, metallicity, and log_g are specified
    if (Teff is not None) and (metallicity is not None) and (log_g is not None):
        v0, v1, v2 = (Teff, metallicity, log_g)
        if 'bosz' in catname.lower():
            sp = call_bosz(v0,v1,v2,**kwargs)
        else:
            if ('ck04models' in catname.lower()) and (v0<3500):
                _log.warning("ck04 models stop at 3500K. Setting Teff=3500.")
                v0 = 3500
            sp = s_ext.Icat(catname, v0, v1, v2)
        sp.name = '({:.0f},{:0.1f},{:0.1f})'.format(v0,v1,v2)
    elif 'flat' in sptype.lower():
        # waveset = s_ext.refs._default_waveset
        # sp = s_ext.ArraySpectrum(waveset, 0*waveset + 10.)
        sp = s_ext.FlatSpectrum(10, fluxunits='photlam')
        sp.name = 'Flat spectrum in photlam'
    elif sptype in sptype_list:
        v0, v1, v2 = lookuptable[sptype]

        if 'bosz' in catname.lower():
            sp = call_bosz(v0,v1,v2,**kwargs)
        else:
            if ('ck04models' in catname.lower()) and (v0<3500):
                _log.warning("ck04 models start at 3500K. Setting Teff=3500.")
                v0 = 3500
            sp = s_ext.Icat(catname, v0, v1, v2)
        sp.name = sptype
    else: # Interpolate values for undefined sptype
        # Sort the list and return their rank values
        sptype_list.sort(key=sort_sptype)
        rank_list = np.array([sort_sptype(st) for st in sptype_list])
        # Find the rank of the input spec type
        rank = sort_sptype(sptype)
        # Grab values from tuples and interpolate based on rank
        tup_list0 = np.array([lookuptable[st][0] for st in sptype_list])
        tup_list1 = np.array([lookuptable[st][1] for st in sptype_list])
        tup_list2 = np.array([lookuptable[st][2] for st in sptype_list])
        v0 = np.interp(rank, rank_list, tup_list0)
        v1 = np.interp(rank, rank_list, tup_list1)
        v2 = np.interp(rank, rank_list, tup_list2)

        if 'bosz' in catname.lower():
            sp = call_bosz(v0,v1,v2,**kwargs)
        else:
            if ('ck04models' in catname.lower()) and (v0<3500):
                _log.warning("ck04 models stop at 3500K. Setting Teff=3500.")
                v0 = 3500
            sp = s_ext.Icat(catname, v0, v1, v2)
        sp.name = sptype

    #print(int(v0),v1,v2)

    # Renormalize if renorm_args exist
    if len(renorm_args) > 0:
        sp_norm = sp.renorm(*renorm_args)
        sp_norm.name = sp.name
        sp = sp_norm

    return sp

def download_votable(target_name, radius=1, **kwargs):
    """Download a VOTable from the Vizier archive

    Reads from Vizier URL: 
    
        f'https://vizier.cds.unistra.fr/viz-bin/sed?-c={target}&-c.rs={radius}'

    Parameters
    ----------
    target_name : str
        Name of target to search for in Vizier.
        Will replace spaces with '+'.
    radius : float
        Search radius in arcseconds.

    Returns
    -------
    astropy.table.Table
    """
    from astropy.table import Table

    target = target_name.replace(' ' ,'+')
    tbl = Table.read(f"https://vizier.cds.unistra.fr/viz-bin/sed?-c={target}&-c.rs={radius}")

    return tbl


# Class for creating an input source spectrum
class source_spectrum(object):
    """Model source spectrum

    The class ingests spectral information of a given target
    and generates :class:`s_ext.Spectrum` model fit to the
    known photometric SED. Two model routines can fit. The
    first is a very simple scale factor that is applied to the
    input spectrum, while the second takes the input spectrum
    and adds an IR excess modeled as a modified blackbody function.

    Parameters
    ----------
    name : string
        Source name.
    sptype : string
        Assumed stellar spectral type. Not relevant if Teff, metallicity,
        and log_g are specified.
    mag_val : float
        Magnitude of input bandpass for initial scaling of spectrum.
    bp : :class:`s_ext.Bandpass`
        Bandpass to apply initial mag_val scaling.
    votable_input: string
        VOTable name that holds the source's photometry. The user can
        find the relevant data at http://vizier.u-strasbg.fr/vizier/sed/
        and click download data.
        Or astropy Table with column 'sed_freq', 'sed_flux', 'sed_eflux'.

    Keyword Args
    ------------
    Teff : float
        Effective temperature ranging from 3500K to 30000K.
    metallicity : float
        Metallicity [Fe/H] value ranging from -2.5 to 0.5.
    log_g : float
        Surface gravity (log g) from 0 to 5.
    Av : float
        Add extinction to the stellar spectrum
    catname : str
        Catalog name, including 'bosz', 'ck04models', and 'phoenix'.
        Default is 'bosz', which comes from :func:`BOSZ_spectrum`.
    res : str
        Spectral resolution to use (200 or 2000 or 20000).
    interpolate : bool
        Interpolate spectrum using a weighted average of grid points
        surrounding the desired input parameters. Default: True
    radius : float
        Search radius in arcseconds for Vizier query.
        Default: 1 arcsec.

    Example
    -------
    Generate a source spectrum and fit photometric data

    >>> import webbpsf_ext
    >>> from webbpsf_ext.spectra import source_spectrum
    >>>
    >>> name = 'HR8799'
    >>> vot = 'votables/{}.vot'.format(name)
    >>> bp_k = webbpsf_ext.bp_2mass('k')
    >>>
    >>> # Read in stellar spectrum model and normalize to Ks = 5.24
    >>> src = source_spectrum(name, 'F0V', 5.24, bp_k, vot,
    >>>                       Teff=7430, metallicity=-0.47, log_g=4.35)
    >>> # Fit model to photometry from 0.1 - 30 microns
    >>> # Saves synphot spectral object at src.sp_model
    >>> src.fit_SED(wlim=[0.1,30])
    >>> sp_sci = src.sp_model

    """

    def __init__(self, name, sptype, mag_val, bp, votable_input,
                 Teff=None, metallicity=None, log_g=None, Av=None, **kwargs):

        self.name = name

        # Setup initial spectrum
        kwargs['Teff']        = Teff
        kwargs['metallicity'] = metallicity
        kwargs['log_g']       = log_g
        self.sp0 = stellar_spectrum(sptype, mag_val, 'vegamag', bp, **kwargs)

        # Read in a low res version for photometry matching
        kwargs['res'] = 200 if not kwargs.get('use_2024', True) else 500
        self.sp_lowres = stellar_spectrum(sptype, mag_val, 'vegamag', bp, **kwargs)

        if Av is not None:
            Rv = 4
            self.sp0 = self.sp0 * s_ext.Extinction(Av/Rv,name='mwrv4')
            self.sp_lowres = self.sp_lowres * s_ext.Extinction(Av/Rv,name='mwrv4')

            self.sp0 = self.sp0.renorm(mag_val, 'vegamag', bp)
            self.sp_lowres = self.sp_lowres.renorm(mag_val, 'vegamag', bp)

            self.sp0.name = sptype
            self.sp_lowres.name = sptype

        # Init model to None
        self.sp_model = None

        # Backwards compatible with votable_file
        if kwargs.get('votable_file') is not None:
            self.votable_input = kwargs.get('votable_file')
        else:
            self.votable_input = votable_input
        
        # Read in photometry
        self._check_file(**kwargs)
        self._gen_table()
        self._combine_fluxes()

    def _check_file(self, **kwargs):
        """Check if VOTable exists. If not, download from Vizier."""

        # Check if votable_input is a string
        is_str = isinstance(self.votable_input, str)

        # If VOTable does not exist, download from Vizier.
        # Stores input as astropy table.
        if is_str and (not os.path.exists(self.votable_input)):
            _log.warning('VOTable does not exist. Downloading from Vizier...')
            tbl = download_votable(self.name, **kwargs)

            # Save VOTable to file
            save_name = self.votable_input
            _log.info('Writing VOTable to {}'.format(save_name))
            tbl.write(save_name, format='votable')

            self.votable_input = tbl

    def _gen_table(self):
        """Read VOTable and convert to astropy table"""

        if isinstance(self.votable_input, str):
            # Import source SED from VOTable
            from astropy.io.votable import parse_single_table
            table = parse_single_table(self.votable_input)
            # Convert to astropy table
            tbl = table.to_table()
        else:
            # Otherwise assume already astropy Table
            tbl = self.votable_input

        # Convert from frequency to wavelength
        freq = tbl['sed_freq']
        wave_A = freq.to(u.Angstrom, equivalencies=u.spectral())

        # Add wavelength column
        col = tbl.Column(wave_A, 'sed_wave')
        tbl.add_column(col)

        # Sort flux monotomically with wavelength
        tbl.sort(['sed_wave', 'sed_flux'])

        self.table = tbl

    def _combine_fluxes(self):
        """Average duplicate data points

        Creates average of duplicate points stored in self.sp_phot.
        """

        table = self.table

        wave = table['sed_wave']
        flux = table["sed_flux"]
        eflux = table["sed_eflux"]

        # Average duplicate data points
        uwave, ucnt = np.unique(wave, return_counts=True)
        uflux = []
        uflux_e = []
        for i, w in enumerate(uwave):
            ind = (wave==w)
            flx = np.median(flux[ind]) if ucnt[i]>1 else flux[ind][0]
            uflux.append(flx)

            eflx = medabsdev(flux[ind]) if ucnt[i]>1 else eflux[ind][0]
            uflux_e.append(eflx)
        uflux = np.array(uflux)
        uflux_e = np.array(uflux_e)

        # Check if uwave and uflux are astropy units
        if isinstance(uwave, table.Column):
            uwave = uwave.value
        if isinstance(uflux, table.Column):
            uflux = uflux.value
        if isinstance(uflux, table.Column):
            uflux_e = uflux_e.value

        # Photometric data points
        sp_phot = s_ext.ArraySpectrum(uwave, uflux,
                                      waveunits=wave.unit.name,
                                      fluxunits=flux.unit.name)
        sp_phot.convert(self.sp_lowres.waveunits)
        sp_phot.convert(self.sp_lowres.fluxunits)

        sp_phot_e = s_ext.ArraySpectrum(uwave, uflux_e,
                                    waveunits=wave.unit.name,
                                    fluxunits=eflux.unit.name)
        sp_phot_e.convert(self.sp_lowres.waveunits)
        sp_phot_e.convert(self.sp_lowres.fluxunits)

        self.sp_phot = sp_phot
        self.sp_phot_e = sp_phot_e


    def bb_jy(self, wave, T):
        """Blackbody function (Jy)

        For a given wavelength set (in um) and a Temperature (K),
        return the blackbody curve in units of Jy.

        Parameters
        ----------
        wave : array_like
            Wavelength array in microns
        T : float
            Temperature of blackbody (K)
        """

        # Physical Constants
        #H  = 6.62620000E-27  # Planck's constant in cgs units
        HS = 6.62620000E-34  # Planck's constant in standard units
        C  = 2.99792458E+08  # speed of light in standard units
        K  = 1.38064852E-23  # Boltzmann constant in standard units

        # Blackbody coefficients (SI units)
        C1 = 2.0 * HS * C    # Power * unit area / steradian
        C2 = HS * C / K

        w_m = wave * 1e-6

        exponent = C2 / (w_m * T)
        expfactor = np.exp(exponent)

        return 1.0E+26 * C1 * (w_m**-3.0) / (expfactor - 1.0)


    def model_scale(self, x, sp=None):
        """Simple model to scale stellar spectrum"""

        sp = self.sp_lowres if sp is None else sp
        return x[0] * sp

    def model_IRexcess(self, x, sp=None):
        """Model for stellar spectrum with IR excess

        Model of a stellar spectrum plus IR excess, where the
        excess is a modified blackbody. The final model follows
        the form:

        .. math::

            x_0 BB(\lambda, x_1) \lambda^{x_2}
        """

        sp = self.sp_lowres if sp is None else sp

        wave = sp.wave

        bb_flux = x[0] * self.bb_jy(wave/1e4, x[1]) * (wave/1e4)**x[2] / 1e17
        sp_bb = s_ext.ArraySpectrum(wave, bb_flux, fluxunits='Jy')
        sp_bb.convert(sp.fluxunits)

        return sp + sp_bb


    def func_resid(self, x, IR_excess=False, wlim=[0.1, 30], use_err=True):
        """Calculate model residuals

        Parameters
        ----------
        x : array_like
            Model parameters for either `model_scale` or `model_IRexcess`.
            See these two functions for more details.
        IR_excess: bool
            Include IR excess in model fit? This is a simple modified blackbody.
        wlim : array_like
            Min and max limits for wavelengths to consider (microns).
        use_err : bool
            Should we use the uncertainties in the SED photometry for weighting?
        """

        # Star model and photometric data
        sp_star = self.sp_lowres
        sp_phot = self.sp_phot
        sp_phot_e = self.sp_phot_e

        # Which model are we using?
        func_model = self.model_IRexcess if IR_excess else self.model_scale

        sp_model = func_model(x, sp_star)

        wvals = sp_phot.wave
        wmin, wmax = np.array(wlim)*1e4
        ind = (wvals >= wmin) & (wvals <= wmax)

        wvals = wvals[ind]
        yvals = sp_phot.flux[ind]
        evals = sp_phot_e.flux[ind]

        # Instead of interpolating on a high-resolution grid,
        # we should really rebin onto a more coarse grid.
        mod_interp = np.interp(wvals, sp_star.wave, sp_model.flux)

        # Normalize values so the residuals aren't super small/large
        norm = np.nanmedian(yvals)

        resid = (mod_interp - yvals)
        if use_err: resid /= evals

        # Return non-NaN normalized values
        return resid[~np.isnan(resid)] / norm

    def fit_SED(self, x0=None, robust=True, use_err=True, IR_excess=False,
                 wlim=[0.3,10], verbose=True):

        """Fit a model function to photometry

        Use :func:`scipy.optimize.least_squares` to find the best fit
        model to the observed photometric data. If no parameters passed,
        then defaults are set.

        Keyword Args
        ------------
        x0 : array_like
            Initial guess of independent variables.
        robust : bool
            Perform an outlier-resistant fit.
        use_err : bool
            Should we use the uncertainties in the SED photometry for weighting?
        IR_excess: bool
            Include IR excess in model fit? This is a simple modified blackbody.
        wlim : array_like
            Min and max limits for wavelengths to consider (microns).
        verbose : bool
            Print out best-fit model parameters. Defalt is True.
        """

        from scipy.optimize import least_squares

        # Default initial starting parameters
        if x0 is None:
            x0 = [1.0, 2000.0, 0.5] if IR_excess else [1.0]

        # Robust fit?
        loss = 'soft_l1' if robust else 'linear'

        # Perform least-squares fit
        kwargs={'IR_excess':IR_excess, 'wlim':wlim, 'use_err':use_err}
        res = least_squares(self.func_resid, x0, bounds=(0,np.inf), loss=loss,
                            kwargs=kwargs)
        out = res.x
        if verbose: 
            print('SED best-fit params:', out)

        # Which model are we using?
        func_model = self.model_IRexcess if IR_excess else self.model_scale
        # Create final model spectrum
        sp_model = func_model(out, self.sp0)
        sp_model.name = self.name

        self.sp_model = sp_model

    @plt.style.context('webbpsf_ext.wext_style')
    def plot_SED(self, ax=None, return_figax=False, xr=[0.3,30], yr=None,
                     units='Jy', **kwargs):

        sp0 = self.sp0
        sp_phot = self.sp_phot
        sp_phot_e = self.sp_phot_e
        sp_model = self.sp_model

        # Convert to Jy and save original units
        sp0_units = sp0.fluxunits.name
        sp_phot_units = sp_phot.fluxunits.name

        # nuFnu or lamFlam?
        if (units=='nufnu') or (units=='lamflam'):
            units = 'flam'
            lfl = True
        else:
            lfl = False

        sp0.convert(units)
        sp_phot.convert(units)

        if ax is None:
            fig, ax = plt.subplots(1,1, figsize=(8,5))

        w = sp0.wave / 1e4
        f = sp0.flux
        if lfl:
            f = f * sp0.wave
        if xr is not None:
            ind = (w>=xr[0]) & (w<=xr[1])
            w, f = (w[ind], f[ind])
        ax.loglog(w, f, lw=1, label='Photosphere', **kwargs)

        w = sp_phot.wave / 1e4
        f = sp_phot.flux
        f_err = sp_phot_e.flux
        if lfl:
            f = f * sp_phot.wave
            f_err = f_err * sp_phot.wave
        if xr is not None:
            ind = (w>=xr[0]) & (w<=xr[1])
            w, f, f_err = (w[ind], f[ind], f_err[ind])
        ax.errorbar(w, f, yerr=f_err, marker='.', ls='none', label='Photometry')

        if sp_model is not None:
            sp_model_units = sp_model.fluxunits.name
            sp_model.convert(units)

            w = sp_model.wave / 1e4
            f = sp_model.flux
            if lfl:
                f = f * sp_model.wave
            if xr is not None:
                ind = (w>=xr[0]) & (w<=xr[1])
                w, f = (w[ind], f[ind])

            ax.plot(w, f, lw=1, label='Model Fit')
            sp_model.convert(sp_model_units)

        # Labels for various units
        ulabels = {'photlam': u'photons s$^{-1}$ cm$^{-2}$ A$^{-1}$',
                   'photnu' : u'photons s$^{-1}$ cm$^{-2}$ Hz$^{-1}$',
                   'flam'   : u'erg s$^{-1}$ cm$^{-2}$ A$^{-1}$',
                   'fnu'    : u'erg s$^{-1}$ cm$^{-2}$ Hz$^{-1}$',
                   'counts' : u'photons s$^{-1}$',
                  }
        if lfl: # Special case nuFnu or lamFlam
            yunits = u'erg s$^{-1}$ cm$^{-2}$'
        else:
            yunits = ulabels.get(units, units)

        ax.set_xlabel('Wavelength (microns)')
        ax.set_ylabel('Flux ({})'.format(yunits))
        ax.set_title(self.name)

        if xr is not None:
            ax.set_xlim(xr)
        if yr is not None:
            ax.set_ylim(yr)

        # Better formatting of ticks marks
        from matplotlib.ticker import LogLocator, AutoLocator, NullLocator
        from matplotlib.ticker import FuncFormatter, NullFormatter
        formatter = FuncFormatter(lambda y, _: '{:.16g}'.format(y))

        xr = ax.get_xlim()
        if xr[1] < 10*xr[0]:
            ax.xaxis.set_major_locator(AutoLocator())
            ax.xaxis.set_minor_locator(NullLocator())
        else:
            ax.xaxis.set_major_locator(LogLocator())
        ax.xaxis.set_major_formatter(formatter)

        yr = ax.get_ylim()
        if yr[1] < 10*yr[0]:
            ax.yaxis.set_major_locator(AutoLocator())
            ax.yaxis.set_minor_formatter(NullFormatter())
            ax.yaxis.get_major_locator().set_params(nbins=10, steps=[1,10])
        else:
            ax.yaxis.set_major_locator(LogLocator())
        ax.yaxis.set_major_formatter(formatter)

        ax.legend()

        # Convert back to original units
        sp0.convert(sp0_units)
        sp_phot.convert(sp_phot_units)

        if ax is None:
            fig.tight_layout()
            if return_figax: return (fig,ax)



# Class for reading in planet spectra
class planets_sb12(object):
    """Exoplanet spectrum from Spiegel & Burrows (2012)

    This contains 1680 files, one for each of 4 atmosphere types, each of
    15 masses, and each of 28 ages.  Wavelength range of 0.8 - 15.0 um at
    moderate resolution (R ~ 204).

    The flux in the source files are at 10 pc. If the distance is specified,
    then the flux will be scaled accordingly. This is also true if the distance
    is changed by the user. All other properties (atmo, mass, age, entropy) are
    not adjustable once loaded.

    Parameters
    ----------
    atmo: str
        A string consisting of one of four atmosphere types:

            - 'hy1s' = hybrid clouds, solar abundances
            - 'hy3s' = hybrid clouds, 3x solar abundances
            - 'cf1s' = cloud-free, solar abundances
            - 'cf3s' = cloud-free, 3x solar abundances

    mass: float
        A number 1 to 15 Jupiter masses (increments of 1MJup).
    age: float
        Age in millions of years (1-1000)
    entropy: float
        Initial entropy (8.0-13.0) in increments of 0.25
    distance: float
        Assumed distance in pc (default is 10pc)
    accr : bool
        Include accretion (default: False)?
    mmdot : float
        From Zhu et al. (2015), the Mjup^2/yr value.
        If set to None then calculated from age and mass.
    mdot : float
        Or use mdot (Mjup/yr) instead of mmdot.
    accr_rin : float
        Inner radius of accretion disk (units of RJup; default: 2)
    truncated: bool
         Full disk or truncated (ie., MRI; default: False)?
    base_dir: str, None
        Location of atmospheric model sub-directories.
    """

	# Define default self.base_dir
    _base_dir = os.path.join(_spec_dir, 'spiegel/')

    def __init__(self, atmo='hy1s', mass=1, age=100, entropy=10.0, distance=10,
                 accr=False, mmdot=None, mdot=None, accr_rin=2.0, truncated=False,
                 base_dir=None, **kwargs):

        self._atmo = atmo
        self._mass = mass
        self._age = age
        self._entropy = entropy

        # Directory of atmospheric files
        if base_dir is not None:
            self._base_dir = base_dir

        # Download and extract tar.gz file if directory does not exist
        if not os.path.isdir(self.sub_dir):
            tar_filename = f'SB.{self.atmo}.tar.gz'
            self._download_tar(extract=True, remove=True, tar_filename=tar_filename)

        # Find and read appropriate file
        self._get_file()
        self._read_file()
        self.distance = distance

        self.accr = accr
        if not accr:
            self.mmdot = 0
        elif mmdot is not None:
            self.mmdot = mmdot
        elif mdot is not None:
            self.mmdot = self.mass * mdot # MJup^2/yr
        else:
            mdot = self.mass / (1e6 * self.age) # Assumed MJup/yr
            self.mmdot = self.mass * mdot # MJup^2/yr

        self.rin = accr_rin
        self.truncated = truncated

    def _download_tar(self, extract=True, remove=True, tar_filename=None):
        """Download and extract tar file"""
        import requests

        if tar_filename is None:
            tar_filename = f'SB.{self.atmo}.tar.gz'

        tar_path = os.path.join(self._base_dir, tar_filename)

        # check if tar file already exists
        if not os.path.exists(tar_path):
            # URL location
            url_base = 'http://mips.as.arizona.edu/~jleisenring/spiegel/'
            url_path = os.path.join(url_base, tar_filename)

            # Make request
            _log.info(f'Downloading file: {tar_filename}')
            req = requests.get(url_path, allow_redirects=True)

            # Raise exception if file not found or other HTTP error
            if req.status_code != requests.codes.ok:
                req.raise_for_status()

            # Save file to directory
            _log.info(f'Saving file to: {tar_path}')
            open(tar_path, 'wb').write(req.content)

        if extract:
            self._extract_dir(tar_path=tar_path, remove=remove)

    def _extract_dir(self, tar_path=None, remove=False):
        """Check if directory exists or is still .tar.gz"""
        import tarfile

        if tar_path is None:
            tar_path = os.path.join(self._base_dir, f'SB.{self.atmo}.tar.gz')

        if not os.path.isdir(self.sub_dir):
            tar = tarfile.open(tar_path, "r:gz")
            tar.extractall(self._base_dir)
            tar.close()

        # Remove tar file
        if remove:
            try: 
                os.remove(tar_path)
            except FileNotFoundError: 
                pass

    def _get_file(self):
        """Find the file closest to the input parameters"""
        files = []; masses = []; ages = []
        for file in os.listdir(self.sub_dir):
            files.append(file)
            fsplit = re.split('[_\.]',file)
            ind_mass = fsplit.index('mass') + 1
            ind_age = fsplit.index('age') + 1
            masses.append(int(fsplit[ind_mass]))
            ages.append(int(fsplit[ind_age]))
        files = np.array(files)
        ages = np.array(ages)
        masses = np.array(masses)

        # Find those indices closest in mass
        mdiff = np.abs(masses - self.mass)
        ind_mass = mdiff == np.min(mdiff)

        # Of those masses, find the closest age
        adiff = np.abs(ages - self.age)
        ind_age = adiff[ind_mass] == np.min(adiff[ind_mass])

        # Get the final file name
        self.file = ((files[ind_mass])[ind_age])[0]

        # Update attributes
        self._mass = masses[ind_mass][0]
        self._age = ages[ind_mass][ind_age][0]

    def _read_file(self):
        """Read in the file data"""

        # Read in the file's content row-by-row (saved as a string)
        file_path = os.path.join(self.sub_dir, self.file)
        with open(file_path) as f:
            content = f.readlines()
        content = [x.strip('\n') for x in content]

        # Parse the strings into an array
        #   Row #, Value
        #   1      col 1: age (Myr);
        #          cols 2-601: wavelength (in microns, in range 0.8-15.0)
        #   2-end  col 1: initial S;
        #          cols 2-601: F_nu (in mJy for a source at 10 pc)

        ncol = len(content[0].split())
        nrow = len(content)
        arr = np.zeros([nrow,ncol])
        for i,row in enumerate(content):
            arr[i,:] = np.array(row.split(), dtype='float64')

        # Find the closest entropy and save
        entropy = arr[1:,0]
        diff = np.abs(self.entropy - entropy)
        ind = diff == np.min(diff)
        # Update entropy attribute
        self._entropy = entropy[ind][0]

        # Get Fluxes
        self._flux = arr[1:,1:][ind,:].flatten()
        self._fluxunits = 'mJy'

        # Save the wavelength information
        self._wave = arr[0,1:]
        self._waveunits = 'um'

        # Distance (10 pc)
        self._distance = 10.0

    @property 
    def sub_dir(self):
        return os.path.join(self._base_dir, f'SB.{self.atmo}')

    @property
    def mdot(self):
        """Accretion rate in MJup/yr"""
        return self.mmdot / self.mass

    @property
    def wave(self):
        """Wavelength of spectrum"""
        return self._wave
    @property
    def waveunits(self):
        """Wavelength units"""
        return self._waveunits

    @property
    def flux(self):
        """Spectral flux"""
        return self._flux
    @property
    def fluxunits(self):
        """Flux units"""
        return self._fluxunits

    @property
    def distance(self):
        """Assumed distance to source (pc)"""
        return self._distance
    @distance.setter
    def distance(self, value):
        self._flux = self._flux * (self._distance/value)**2
        self._distance = value

    @property
    def atmo(self):
        """Atmosphere type
        """
        return self._atmo
    @property
    def mass(self):
        """Mass of planet (MJup)"""
        return self._mass
    @property
    def age(self):
        """Age in millions of years"""
        return self._age
    @property
    def entropy(self):
        """Initial entropy (8.0-13.0)"""
        return self._entropy
    
    def export_pysynphot(self, **kwargs):
        _log.warning("export_pysynphot() is deprecated. Use export_synphot() instead.")
        return self.export_synphot(**kwargs)

    def export_synphot(self, waveout='angstrom', fluxout=None):
        """Output to synphot spectrum object

        Parameters
        ----------
        waveout : str
            Wavelength units for output
        fluxout : str
            Flux units for output
        """
        w = self.wave; f = self.flux
        name = (re.split('[\.]', self.file))[0]#[5:]
        sp = s_ext.ArraySpectrum(w, f, name=name, waveunits=self.waveunits, fluxunits=self.fluxunits)

        sp.convert(waveout)
        if fluxout is not None:
            sp.convert(fluxout)

        if self.accr and (self.mmdot>0):
            sp_mdot = sp_accr(self.mmdot, rin=self.rin,
                              dist=self.distance, truncated=self.truncated,
                              waveout=waveout, fluxout=fluxout)
            # Interpolate accretion spectrum at each wavelength
            # and create new composite spectrum
            fnew = np.interp(sp.wave, sp_mdot.wave, sp_mdot.flux)
            sp_new = s_ext.ArraySpectrum(sp.wave, sp.flux+fnew,
                                     waveunits=waveout, fluxunits=fluxout)
            return sp_new
        else:
            return sp

def sp_accr(mmdot, rin=2, dist=10, truncated=False,
            waveout='angstrom', fluxout='photlam', base_dir=None):

    """Exoplanet accretion flux values (Zhu et al., 2015).

    Calculated the wavelength-dependent flux of an exoplanet accretion disk/shock
    from Zhu et al. (2015). 

    Note
    ----
    This function only uses the table of photometric values to calculate
    photometric brightness from a source, so not very useful for simulating
    spectral observations.


    Parameters
    ----------
    mmdot : float
        Product of the exoplanet mass and mass accretion rate (MJup^2/yr).
        Values range from 1e-7 to 1e-2.
    rin : float
        Inner radius of accretion disk (units of RJup; default: 2).
    dist : float
        Distance to object (pc).
    truncated: bool
        If True, then the values are for a disk with Rout=50 RJup,
        otherwise, values were calculated for a full disk (Rout=1000 RJup).
        Accretion from a "tuncated disk" is due mainly to MRI.
        Luminosities for full and truncated disks are very similar.
    waveout : str
        Wavelength units for output
    fluxout : str
        Flux units for output
    base_dir: str, None
        Location of accretion model sub-directories.
    """

    base_dir = _spec_dir if base_dir is None else base_dir
    fname = os.path.join(base_dir, 'zhu15_accr.txt')

    names = ('MMdot', 'Rin', 'Tmax', 'J', 'H', 'K', 'L', 'M', 'N', 'J2', 'H2', 'K2', 'L2', 'M2', 'N2')
    tbl = ascii.read(fname, guess=True, names=names)

    # Inner radius values and Mdot values
    rin_vals = np.unique(tbl['Rin'])
    mdot_vals = np.unique(tbl['MMdot'])
    nmdot = len(mdot_vals)

    assert (rin >=rin_vals.min())  & (rin <=rin_vals.max()), "rin is out of range"
    assert (mmdot>=mdot_vals.min()) & (mmdot<=mdot_vals.max()), "mmdot is out of range"

    if truncated:
        mag_names = ('J2', 'H2', 'K2', 'L2', 'M2', 'N2')
    else:
        mag_names = ('J', 'H', 'K', 'L', 'M', 'N')
    wcen = np.array([ 1.2,  1.6, 2.2, 3.8, 4.8, 10.0])
    zpt  = np.array([1600, 1020, 657, 252, 163, 39.8])

    mag_arr = np.zeros([6,nmdot])
    for i, mv in enumerate(mdot_vals):
        for j, mag in enumerate(mag_names):
            tbl_sub = tbl[tbl['MMdot']==mv]
            rinvals = tbl_sub['Rin']
            magvals = tbl_sub[mag]

            mag_arr[j,i] = np.interp(rin, rinvals, magvals)

    mag_vals = np.zeros(6)
    for j in range(6):
        xi = 10**(mmdot)
        xp = 10**(mdot_vals)
        yp = 10**(mag_arr[j])
        mag_vals[j] = np.log10(np.interp(xi, xp, yp))

    mag_vals += 5*np.log10(dist/10)
    flux_Jy = 10**(-mag_vals/2.5) * zpt

    sp = s_ext.ArraySpectrum(wcen*1e4, flux_Jy, fluxunits='Jy')
    sp.convert(waveout)
    sp.convert(fluxout)

    return sp


def jupiter_spec(dist=10, waveout='angstrom', fluxout='photlam', base_dir=None):
    """Jupiter as an Exoplanet
    
    Read in theoretical Jupiter spectrum from Irwin et al. 2014 and output
    as a synphot spectrum.
    
    Parameters
    ===========
    dist : float
        Distance to Jupiter (pc).
    waveout : str
        Wavelength units for output.
    fluxout : str
        Flux units for output.
    base_dir: str, None
        Location of tabulated file irwin_2014_ref_spectra.txt.
    """

    base_dir = os.path.join(_spec_dir, 'solar_system/') if base_dir is None else base_dir
    fname = os.path.join(base_dir, 'irwin_2014_ref_spectra.txt')

    # Column 1: Wavelength (in microns)
    # Column 2: 100*Ap/Astar (Earth-Sun Primary Transit)
    # Column 3: 100*Ap/Astar (Earth-Mdwarf Primary Transit)
    # Column 4: 100*Ap/Astar (Jupiter-Sun Primary Transit)
    # Column 5: Fp/Astar (Earth-Sun Secondary Eclipse)
    # Column 6: Disc-averaged radiance of Earth (W cm-2 sr-1 micron-1)
    # Column 7: Fp/Astar (Jupiter-Sun Secondary Eclipse)
    # Column 8: Disc-averaged radiance of Jupiter (W cm-2 sr-1 micron-1)
    # Column 9: Solar spectral irradiance spectrum (W micron-1)
    #            (Solar Radius = 695500.0 km)
    # Column 10: Mdwarf spectral irradiance spectrum (W micron-1)
    #            (Mdwarf Radius = 97995.0 km)

    data = ascii.read(fname, data_start=14)

    wspec = data['col1'] * 1e4 # Angstrom
    fspec = data['col8'] * 1e3 # erg s-1 cm^-2 A^-1 sr^-1
    
    # Steradians to square arcsec
    sr_to_asec2 = (3600*180/np.pi)**2
    fspec /= sr_to_asec2       # *** / arcsec^2

    # Angular size of Jupiter at some distance
    RJup_km = 71492.0
    au_to_km = 149597870.7
    # Angular size (arcsec) of Jupiter radius
    RJup_asec = RJup_km / au_to_km / dist
    area = np.pi * RJup_asec**2
    
    # flux in f_lambda
    fspec *= area        # erg s-1 cm^-2 A^-1

    sp = s_ext.ArraySpectrum(wspec, fspec, fluxunits='flam')
    sp.convert(waveout)
    sp.convert(fluxout)
    
    return sp


def linder_table(file=None, **kwargs):
    """Load Linder Model Table

    Function to read in isochrone models from Linder et al. 2019.
    Returns an astropy Table.

    Parameters
    ----------
    file : string
        Location and name of Linder et al file. 
        Default is ``BEX_evol_mags_-3_MH_0.00.dat``.
    """

    # Default input directory
    indir = os.path.join(_spec_dir, 'linder', 'isochrones/')
    # Default file
    if file is None:
        file = 'BEX_evol_mags_-3_MH_0.00.dat'

    # First check if file is in indir
    file_path = os.path.join(indir, file)
    if not os.path.exists(file_path):
        # Check if file is in current directory
        file_path = file
        if not os.path.exists(file):
            raise ValueError(f"File {file_path} not found.")

    with open(file_path) as f:
        content = f.readlines()

    content = [x.strip('\n') for x in content]

    cnames = content[2].split(',')
    cnames = [name.split(':')[1] for name in cnames]
    ncol = len(cnames)
    
    content_arr = []
    for line in content[4:]:
        arr = np.array(line.split()).astype(float)
        if len(arr)>0: 
            content_arr.append(arr)
    
    content_arr = np.array(content_arr)

    # Convert to Astropy Table
    tbl = Table(rows=content_arr, names=cnames)
    
    return tbl
    
def linder_filter(table, filt, age, dist=10, cond_file=None, 
                  extrapolate=True, **kwargs):
    """Linder Mags vs Mass Arrays
    
    Given a Linder table, filter name, and age (Myr), return arrays of MJup 
    and Vega mags. If distance (pc) is provided, then return the apparent 
    magnitude, otherwise absolute magnitude at 10pc.
    
    This function takes the isochrones tables from Linder et al 2019 and
    creates a irregular contour grid of filter magnitude and log(age)
    where the z-axis is log(mass). This is mapped onto a regular grid
    that is interpolated within the data boundaries and linearly
    extrapolated outside of the region of available data.
    
    Parameters
    ==========
    table : astropy table
        Astropy table output from `linder_table`.
    filt : string
        Name of NIRCam filter.
    age : float
        Age in Myr of planet.

    Keyword Args
    =============
    dist : float
        Distance in pc. Default is 10pc (abs mag).
    cond_file : string
        COND file to use for extrapolating to higher masses.
    extrapolate : bool
        If True, extrapolate to higher masses using COND models
        as well as lower masses using a lower order polynomial fit.
    """

    from .maths import jl_poly, jl_poly_fit
    
    # In the event of underscores within name
    filt = filt.split('_')[0]

    try:
        x = table[filt]
    except KeyError:
        # In case specific filter doesn't exist, interpolate
        x = []
        cnames = [
            'SPHEREY', 'NACOJ', 'NACOH', 'NACOKs', 'NACOLp', 'NACOMp',
            'F115W', 'F150W', 'F200W', 'F277W', 'F356W', 'F444W', 'F560W',
            'F770W', 'F1000W', 'F1280W', 'F1500W', 'F1800W', 'F2100W', 'F2550W',
        ]
        wvals = np.array([
            1.04, 1.27, 1.66, 2.20, 3.80, 4.80,
            1.15, 1.50, 2.00, 2.76, 3.57, 4.41, 5.60,
            7.67, 9.97, 12.84, 15.08, 17.99, 20.85, 25.27,
        ])
                          
        # Sort by wavelength 
        isort = np.argsort(wvals)
        cnames = list(np.array(cnames)[isort])
        wvals = wvals[isort]

        # Turn table data into array and interpolate at filter wavelength
        tbl_arr = np.array([table[cn].data for cn in cnames]).transpose()
        try:
            bp = nircam_filter(filt)
        except:
            bp = miri_filter(filt)
        wint = bp.avgwave().to_value('um')
        x = np.array([np.interp(wint, wvals, row) for row in tbl_arr])
        
    y = table['log(Age/yr)'].data
    z = table['Mass/Mearth'].data
    zlog = np.log10(z)

    #######################################################
    # Grab COND model data to fill in higher masses
    if extrapolate:
        base_dir = os.path.join(_spec_dir, 'cond_models/')
        if cond_file is None: 
            cond_file = os.path.join(base_dir, 'model.AMES-Cond-2000.M-0.0.JWST.Vega')
        npsave_file = os.path.join(cond_file + f'.{filt}.npy')
    
        if os.path.exists(npsave_file):
            mag2, age2, mass2_mjup = np.load(npsave_file)
        else:
            d_tbl2 = cond_table(file=cond_file) # Dictionary of ages
            mass2_mjup = []
            mag2 = []
            age2 = []
            for k in d_tbl2.keys():
                tbl2 = d_tbl2[k]
                mass2_mjup = mass2_mjup + list(tbl2['MJup'].data)
                try:
                    mag2 = mag2 + list(tbl2[filt+'a'].data) # NIRCam
                except KeyError:        
                    filt_alt = {'F1065C':'F1000W', 'F1140C':'F1130W', 'F1550C':'F1500W', 'F2300C':'F2100W'}
                    fcol = filt_alt.get(filt, filt)
                    mag2 = mag2 + list(tbl2[fcol].data)  # MIRI
                age2 = age2 + list(np.ones(len(tbl2))*k)
        
            mass2_mjup = np.array(mass2_mjup)
            mag2 = np.array(mag2)
            age2 = np.array(age2)
        
            mag_age_mass = np.array([mag2,age2,mass2_mjup])
            np.save(npsave_file, mag_age_mass)    

        # Irregular grid
        x2 = mag2
        age2 = age2 * 1e6 # Convert from Myr to years
        y2 = np.log10(age2)
        z2 = mass2_mjup * 318 # Convert to Earth masses
        zlog2 = np.log10(z2)


    #######################################################
    
    if extrapolate:
        xlim = np.array([x2.min(),x.max()+5]) # magnitude limits
        ylim = np.array([6,10])  # 10^6 to 10^10 yrs
    else:
        xlim = np.array([x.min(),x.max()]) # magnitude limits
        ylim = np.array([y.min(),y.max()]) # age limits
    dx = (xlim[1] - xlim[0]) / 200
    dy = (ylim[1] - ylim[0]) / 200
    xgrid = np.arange(xlim[0], xlim[1]+dx, dx)
    ygrid = np.arange(ylim[0], ylim[1]+dy, dy)
    X, Y = np.meshgrid(xgrid, ygrid)
    
    zgrid = griddata((x,y), zlog, (X, Y), method='cubic')
    # There will be NaN's along the border that need to be replaced
    ind_nan = np.isnan(zgrid)
    if extrapolate:
        # Replace some NaNs with COND grid
        zgrid_cond = griddata((x2,y2), zlog2, (X, Y), method='cubic')
        zgrid[ind_nan] = zgrid_cond[ind_nan]
        ind_nan = np.isnan(zgrid)
    
    # Remove rows/cols with NaN's
    # x is mag, y is log(age), z is log(mass)
    # xgrid2, ygrid2, zgrid2 = _trim_nan_array(xgrid, ygrid, zgrid)
    xgrid2, ygrid2, zgrid2 = xgrid, ygrid, zgrid

    # Create regular grid interpolator function for extrapolation at NaN's
    # Need to us linear method over cubic if not trimming NaN's
    fill_value = None if extrapolate else np.nan
    func = RegularGridInterpolator((ygrid2,xgrid2), zgrid2, method='linear',
                                   bounds_error=False, fill_value=fill_value)

    if extrapolate:
        # Fix NaN's in zgrid and rebuild func
        pts = np.array([Y[ind_nan], X[ind_nan]]).transpose()
        zgrid[ind_nan] = func(pts)

        func = RegularGridInterpolator((ygrid,xgrid), zgrid, method='linear',
                                       bounds_error=False, fill_value=None)
    
    # Get mass limits for series of magnitudes at a given age                                
    age_log = np.log10(age*1e6)
    mag_abs_arr = xgrid
    pts = np.array([(age_log,xval) for xval in mag_abs_arr])
    mass_arr = 10**func(pts) / 318.0 # Convert to MJup

    # Get rid of any NaN's
    ind_use = ~np.isnan(mass_arr)
    mag_abs_arr = mag_abs_arr[ind_use]
    mass_arr = mass_arr[ind_use]
    
    # Sort by magnitude
    isort = np.argsort(mag_abs_arr)
    mag_abs_arr = mag_abs_arr[isort]
    mass_arr = mass_arr[isort]

    # At a given magnitude, if there is a lower mass at a brighter magnitude, 
    # replace mass value
    # for i, mag in enumerate(mag_abs_arr):
    #     ind = mag_abs_arr < mag
    #     if ind.sum() > 0:
    #         mass_arr[i] = np.min([mass_arr[i], mass_arr[ind].min()])

    # Fit low order polynomial
    ifit = mag_abs_arr<x.max()
    xfit = np.append(mag_abs_arr[ifit], mag_abs_arr[-1])
    yfit = np.log10(np.append(mass_arr[ifit], mass_arr[-1]))
    # Perform a bunch of polynomial fits and find chi^2 to choose optimal degree
    use_lg = False
    lxmap = None # np.array([mag_abs_arr.min(), mag_abs_arr.max()])
    chi2_arr = []
    deg_arr = np.arange(1,8)
    for deg in deg_arr:
        cf = jl_poly_fit(xfit, yfit, deg=deg, use_legendre=use_lg, lxmap=lxmap)
        vals_fit = 10**jl_poly(mag_abs_arr,cf, use_legendre=use_lg, lxmap=lxmap)
        chi2 = np.sum((mass_arr - vals_fit)**2)
        chi2_arr.append(chi2)
    ind_deg = np.argmin(chi2_arr)
    deg = deg_arr[ind_deg]
    cf = jl_poly_fit(xfit, yfit, deg=deg, use_legendre=use_lg, lxmap=lxmap)
    mass_arr_fit = 10**jl_poly(mag_abs_arr,cf, use_legendre=use_lg, lxmap=lxmap)

    # Convert to apparent magnitude
    mag_app_arr = mag_abs_arr + 5*np.log10(dist/10.0)

    # Sort by mass
    mass_arr_fin = mass_arr_fit
    isort = np.argsort(mass_arr_fin)
    mass_arr_fin = mass_arr_fin[isort]
    mag_app_arr = mag_app_arr[isort]

    return mass_arr_fin, mag_app_arr
    

def cond_table(age=None, file=None, **kwargs):
    """Load COND Model Table

    Function to read in the COND model tables, which have been formatted
    in a very specific way. Has the option to return a dictionary of
    astropy Tables, where each dictionary element corresponds to
    the specific ages within the COND table. Or, if the age keyword is
    specified, then this function only returns a single astropy table.

    Parameters
    ----------
    age : float
        Age in Myr. If set to None, then an array of ages from the file 
        is used to generate dictionary. If set, chooses the closest age
        supplied in table.
    file : string
        Location and name of COND file. See isochrones stored at
        https://phoenix.ens-lyon.fr/Grids/.
        Default is model.AMES-Cond-2000.M-0.0.JWST.Vega
    """

    def make_table(*args):
        i1, i2 = (ind1[i]+4, ind2[i])

        rows = []
        for line in content[i1:i2]:
            if (line=='') or ('---' in line):
                continue
            else:
                vals = np.array(line.split(), dtype='float64')
                rows.append(tuple(vals))
        tbl = Table(rows=rows, names=cnames)

        # Convert to Jupiter masses
        newcol = tbl['M/Ms'] * 1047.348644
        newcol.name = 'MJup'
        tbl.add_column(newcol, index=1)
        tbl['MJup'].format = '.2f'

        return tbl

    # Default input directory
    base_dir = os.path.join(_spec_dir, 'cond_models/')
    # Default file
    if file is None:
        file = 'model.AMES-Cond-2000.M-0.0.JWST.Vega'

    # First check if file is in indir
    file_path = os.path.join(base_dir, file)
    if not os.path.exists(file_path):
        # Check if file is in current directory
        file_path = file
        if not os.path.exists(file):
            raise ValueError(f"File {file_path} not found.")

    with open(file_path) as f:
        content = f.readlines()

    content = [x.strip('\n') for x in content]

    # Column names
    cnames = content[5].split()
    cnames = ['M/Ms', 'Teff'] + cnames[1:]
    ncol = len(cnames)

    # Create a series of tables for each time
    times_gyr = []
    ind1 = []
    for i, line in enumerate(content):
        if 't (Gyr)' in line:
            times_gyr.append(line.split()[-1])
            ind1.append(i)
    ntimes = len(times_gyr)

    # Create start and stop indices for each age value
    ind2 = ind1[1:] + [len(content)]
    ind1 = np.array(ind1)
    ind2 = np.array(ind2)-1

    # Everything is Gyr, but prefer Myr
    ages_gyr = np.array(times_gyr, dtype='float64')
    ages_myr = np.array(ages_gyr * 1000, dtype='int')
    #times = ['{:.0f}'.format(a) for a in ages_myr]

    # Return all tables if no age specified
    if age is None:
        tables = {}
        for i in range(ntimes):
            tbl = make_table(i, ind1, ind2, content)
            tables[ages_myr[i]] = tbl
        return tables
    else:
        # This is faster if we only want one table
        ages_diff = np.abs(ages_myr - age)
        i = np.where(ages_diff==ages_diff.min())[0][0]

        tbl = make_table(i, ind1, ind2, content)
        return tbl

def cond_filter(table, filt, module='A', dist=None, **kwargs):
    """
    Given a COND table and NIRCam filter, return arrays of MJup and Vega mags.
    If distance (pc) is provided, then return the apparent magnitude,
    otherwise absolute magnitude at 10pc. Input table has already been filtered
    by age.
    """

    from .maths import jl_poly, jl_poly_fit

    # Table Data
    try:
        fcol = filt + module.lower()
        mag_data  = table[fcol].data
    except KeyError:
        # MIRI coronagraphic filters are incorrect in the AMES-COND files.
        # It assumes extra attenuation from the central mask, which gives
        # the incorrect flux for off-axis points sources. Instead, use some
        # alternate bandpasses
        filt_alt = {'F1065C':'F1000W', 'F1140C':'F1130W', 'F1550C':'F1500W', 'F2300C':'F2100W'}
        fcol = filt_alt.get(filt, filt)
        mag_data  = table[fcol].data

    mcol = 'MJup'
    mass_data = table[mcol].data

    # Data to interpolate onto
    mass_arr = list(np.arange(0.1,1,0.1)) + list(np.arange(1,10)) \
        + list(np.arange(10,200,10)) + list(np.arange(200,1400,100))
    mass_arr = np.array(mass_arr)

    # Interpolate
    mag_arr = np.interp(mass_arr, mass_data, mag_data)

    # Extrapolate
    cf = jl_poly_fit(np.log(mass_data), mag_data)
    ind_out = (mass_arr < mass_data.min()) | (mass_arr > mass_data.max())
    mag_arr[ind_out] = jl_poly(np.log(mass_arr), cf)[ind_out]

    # Distance modulus for apparent magnitude
    if dist is not None:
        mag_arr = mag_arr + 5*np.log10(dist/10)

    return mass_arr, mag_arr

def mass_sensitivity_table(filt, dist=10, extrapolate=True, lfile=None, age_arr=None, 
                           save=True, save_dir=None, return_tbl=False, **kwargs):
    """ Generate a table of mass sensitivities for a given filter

    Interpolate mass and brightnesses of BEX evolutionary tracks. Creates a
    table with first column of magnitudes, second column is log10(Jy), and
    subsequent columns are log10(Mass) for each age in Myr.


    Parameters
    ----------
    filt : str
        Name of filter
    dist : float
        Distance in parsecs
    extrapolate : bool
        If True, extrapolate to higher masses using COND models
        as well as lower masses using a lower order polynomial fit.
    lfile : str
        Filename of Linder evolutionary tracks. If None, then use
        BEX_evol_mags_-3_MH_0.00.dat (warm start HELIOS grid).
    age_arr : array_like
        Array of ages in Myr. If None, then defualts to [1,2,...,20] Myr.
    save : bool
        Save table to file.
    save_dir : str
        Directory to save table. If None, then save to current directory.
    return_tbl : bool
        Return astropy table object
    """

    from .bandpasses import nircam_filter

    if lfile is None:
        lfile = 'BEX_evol_mags_-3_MH_0.00.dat'
    tbl = linder_table(file=lfile)

    if age_arr is None:
        age_arr = np.arange(1, 21, 1)

    mass_all = []
    mag_all = []
    for age in age_arr:
        mass_data, mag_data = linder_filter(tbl, filt, age, dist, 
                                            extrapolate=extrapolate, **kwargs)
        mass_all.append(mass_data)
        mag_all.append(mag_data)

    # Interpolate masses onto a common magnitude array
    dm = 0.1
    if extrapolate:
        mag_arr_min = np.round(np.max([np.min(m) for m in mag_all])+dm, 1)
        mag_arr_max = np.round(np.max([np.max(m) for m in mag_all])-dm, 1)
    else:
        mag_arr_min = np.round(np.min([np.min(m) for m in mag_all])+dm, 1)
        mag_arr_max = np.round(np.max([np.max(m) for m in mag_all])-dm, 1)
    mag_arr = np.arange(mag_arr_min, mag_arr_max, 0.1)

    # Convert magnitudes to Jy
    # Get 0th magnitude flux density
    bp = nircam_filter(filt)
    sp = stellar_spectrum('G2V', 0, 'vegamag', bp)
    obs = s_ext.Observation(sp, bp, binset=bp.wave)
    flux0 = obs.effstim('Jy')
    fluxJy_arr = 10**(-0.4*mag_arr)*flux0

    mass_arr = []
    for i, mass_vals in enumerate(mass_all):
        # 
        fn = interp1d(mag_all[i], mass_vals, kind='cubic', 
                    bounds_error=False, fill_value=np.nan)
        mass_arr.append(fn(mag_arr))

        # isort = np.argsort(mag_all[i])
        # xvals = mag_all[i][isort]
        # yvals = mass_vals[isort]
        # mass_arr.append(np.interp(mag_arr, xvals, yvals))

    mass_arr = np.array(mass_arr)
    log_jy = np.log10(fluxJy_arr)

    # Create astropy table
    data = np.concatenate([[mag_arr], [log_jy], np.log10(mass_arr)])
    names = ['mag', 'log_Jy'] + [f'{age}Myr' for age in age_arr]
    tbl = Table(data=data.T, names=names)

    tbl['mag'].info.format = '.1f'
    tbl['log_Jy'].info.format = '.3f'
    for k in tbl.colnames[2:]:
        tbl[k].info.format = '.4f'

    if save:
        ext_str = '_extrapolated' if extrapolate else ''
        save_name = f'{filt}__{lfile[:-4]}__d{dist}pc{ext_str}.txt'
        if save_dir is not None:
            save_path = os.path.join(save_dir, save_name)
        else:
            save_path = save_name
        tbl.write(save_path, overwrite=True, format='ascii.fixed_width', 
            bookend=False, delimiter=None, delimiter_pad='  ')

    if return_tbl:
        return tbl


def _trim_nan_array(xgrid, ygrid, zgrid):
    """NaN Trimming of Array Image

    For an image with a rotated border of NaN's,
    remove rows/cols with NaN's while trying to 
    preserve the maximum footprint of real data.
    """

    xgrid2, ygrid2, zgrid2 = xgrid, ygrid, zgrid

    # Create a mask of NaN'ed values
    nan_mask = np.isnan(zgrid2)
    nrows, ncols = nan_mask.shape
    # Determine number of NaN's along each row and col
    num_nans_cols = nan_mask.sum(axis=0)
    num_nans_rows = nan_mask.sum(axis=1)

    # First, crop all rows/cols that are only NaN's
    xind_good = np.where(num_nans_cols < nrows)[0]
    yind_good = np.where(num_nans_rows < ncols)[0]
    # get border limits
    x1, x2 = (xind_good.min(), xind_good.max()+1)
    y1, y2 = (yind_good.min(), yind_good.max()+1)
    # Trim of NaN borders
    xgrid2 = xgrid2[x1:x2]
    ygrid2 = ygrid2[y1:y2]
    zgrid2 = zgrid2[y1:y2,x1:x2]

    # Find a optimal rectangule subsection free of NaN's
    # Iterative cropping
    ndiff = 5
    while np.isnan(zgrid2.sum()):
        # Make sure ndiff is not negative
        if ndiff<0:
            break

        npix = zgrid2.size

        # Create a mask of NaN'ed values
        nan_mask = np.isnan(zgrid2)
        nrows, ncols = nan_mask.shape
        # Determine number of NaN's along each row and col
        num_nans_cols = nan_mask.sum(axis=0)
        num_nans_rows = nan_mask.sum(axis=1)

        # Look for any appreciable diff row-to-row/col-to-col
        col_diff = num_nans_cols - np.roll(num_nans_cols,-1) 
        row_diff = num_nans_rows - np.roll(num_nans_rows,-1)
        # For edge wrapping, just use last minus previous
        col_diff[-1] = col_diff[-2]
        row_diff[-1] = row_diff[-2]
    
        # Keep rows/cols composed mostly of real data 
        # and where number of NaN's don't change dramatically
        xind_good = np.where( ( np.abs(col_diff) <= ndiff  ) & 
                                ( num_nans_cols < 0.5*nrows ) )[0]
        yind_good = np.where( ( np.abs(row_diff) <= ndiff  ) & 
                                ( num_nans_rows < 0.5*ncols ) )[0]
        # get border limits
        x1, x2 = (xind_good.min(), xind_good.max()+1)
        y1, y2 = (yind_good.min(), yind_good.max()+1)

        # Trim of NaN borders
        xgrid2 = xgrid2[x1:x2]
        ygrid2 = ygrid2[y1:y2]
        zgrid2 = zgrid2[y1:y2,x1:x2]
    
        # Check for convergence
        # If we've converged, reduce 
        if npix==zgrid2.size:
            ndiff -= 1
            
    # Last ditch effort in case there are still NaNs
    # If so, remove rows/cols 1 by 1 until no NaNs
    while np.isnan(zgrid2.sum()):
        xgrid2 = xgrid2[1:-1]
        ygrid2 = ygrid2[1:-1]
        zgrid2 = zgrid2[1:-1,1:-1]
        
    return xgrid2, ygrid2, zgrid2

def companion_spec(bandpass, model='SB12', atmo='hy3s', mass=10, age=100, entropy=10,
    dist=10, accr=False, mmdot=None, mdot=None, accr_rin=2, truncated=False,
    sptype=None, renorm_args=None, Av=0, **kwargs):
    """ Create a spectrum of a companion 

    Parameters
    ----------
    bandpass : :class:`s_ext.Bandpass`
        A synphot bandpass object.
    model : str
        Exoplanet model to use ('sb12', 'bex', 'cond') or
        stellar spectrum model ('bosz', 'ck04models', 'phoenix').
    atmo : str
        A string consisting of one of four atmosphere types:
        ['hy1s', 'hy3s', 'cf1s', 'cf3s'].
    mass: int
        Number 1 to 15 Jupiter masses.
    age: float
        Age in millions of years (1-1000).
    entropy: float
        Initial entropy (8.0-13.0) in increments of 0.25

    sptype : str
        Instead of using a exoplanet spectrum, specify a stellar type.
    renorm_args : tuple
        Renormalization arguments in case you want
        very specific luminosity in some bandpass.
        Includes (value, units, bandpass).

    dist : float
        Distance in pc.
    Av : float
        Extinction magnitude (assumes Rv=4.0) of the companion
        (e.g., due to being embedded in a disk).

    accr : bool
        Include accretion? default: False
    mmdot : float
        From Zhu et al. (2015), the Mjup^2/yr value.
        If set to None then calculated from age and mass.
    mdot : float
        Or use mdot (Mjup/yr) instead of mmdot.
    accr_rin : float
        Inner radius of accretion disk (units of RJup; default: 2)
    truncated: bool
         Full disk or truncated (ie., MRI; default: False).

     """
    # Spiegel & Burrows model already have a class
    # For BEX and COND models, make 
    if model.lower() in ['sb12', 'bex', 'cond']:
        calc_accr = False if model.lower() in ['bex', 'cond'] else accr
        pl = {
            'atmo': atmo, 'mass': mass, 'age': age,  
            'entropy': entropy, 'distance': dist,
            'accr': calc_accr, 'mmdot': mmdot, 'mdot': mdot, 
            'accr_rin': accr_rin, 'truncated': truncated
        }
        planet = planets_sb12(**pl)
        sp = planet.export_synphot()
        
        # Check spectral overlap
        sp_overlap = bandpass.check_overlap(sp)

        # Ensure there is a data point at the edge of the input bandpass
        if sp_overlap != 'full':
            w_end = np.max(bandpass.wave)
            f_end = sp(w_end)
            w_new = np.append(sp.wave, w_end)
            f_new = np.append(sp.flux, f_end)
            sp = s_ext.ArraySpectrum(w_new, f_new, waveunits=sp.waveunits, fluxunits=sp.fluxunits)

        del_mag = 0
        # Add accretion mag offsets for BEX and COND models
        if (model.lower() in ['bex', 'cond']) and (accr == True):
            if sp_overlap != 'full':
                _log.warning(f"Overlap between spectrum and bandpass: {sp_overlap}.")
                _log.warning("Accretion calculation may be unreliable.")
            pl = {
                'atmo': atmo, 'mass': mass, 'age': age,
                'entropy': entropy, 'distance': dist,
                'accr': True, 'mmdot': mmdot, 'mdot': mdot,
                'accr_rin': accr_rin, 'truncated': truncated
            }
            planet = planets_sb12(**pl)
            # Get spectrum from accretion component
            sp_mdot = sp_accr(planet.mmdot, rin=planet.rin,
                              dist=planet.distance, truncated=planet.truncated,
                              waveout=sp.waveunits, fluxout=sp.fluxunits)
            # Interpolate accretion spectrum at each wavelength
            fnew = np.interp(sp.wave, sp_mdot.wave, sp_mdot.flux)
            sp_new = s_ext.ArraySpectrum(sp.wave, fnew, waveunits=sp.waveunits,
                                     fluxunits=sp.fluxunits)
            obs_accr = s_ext.Observation(sp_new, bandpass, binset=bandpass.wave)
            del_mag -= obs_accr.effstim('vegamag')
            # Make new spectrum
            sp = planet.export_synphot()

        # Add extinction from the disk
        if Av>0: 
            Rv = 4.0  
            sp_ext = sp * s_ext.Extinction(Av/Rv, name='mwrv4')

            if model.lower() in ['bex', 'cond']:
                if sp_overlap != 'full':
                    _log.warning(f"Overlap between spectrum and bandpass: {sp_overlap}.")
                    _log.warning("Extinction calculation may be unreliable.")
                obs = s_ext.Observation(sp, bandpass, binset=bandpass.wave)
                obs_ext = s_ext.Observation(sp_ext, bandpass, binset=bandpass.wave)
                del_mag += obs_ext.effstim('vegamag') - obs.effstim('vegamag')
            sp = sp_ext
                        
        # For BEX and COND models, set up renorm_args
        # unless renorm_args is already set
        filt = bandpass.name.split('_')[0]
        if (renorm_args is not None) and (len(renorm_args) > 0):
            pass
        elif model.lower()=='bex':
            table = linder_table()
            mass_arr, mag_arr = linder_filter(table, filt, age, dist=dist)
            mag = np.interp(mass, mass_arr, mag_arr)
            mag += del_mag  # Apply extinction and/or accretion offsets
            renorm_args = (mag, 'vegamag', bandpass)
        elif model.lower()=='cond':
            table = cond_table(age)
            mass_arr, mag_arr = cond_filter(table, filt, dist=dist)
            mag = np.interp(mass, mass_arr, mag_arr)
            mag += del_mag  # Apply extinction and/or accretion offsets
            renorm_args = (mag, 'vegamag', bandpass)
            
        # Renormalize to some specified flux in a given bandpass
        if (renorm_args is not None) and (len(renorm_args) > 0):
            sp_norm = sp.renorm(*renorm_args, force=True)
            sp = sp_norm
        elif sp_overlap != 'full':
            _log.warning(f"Overlap between spectrum and bandpass: {sp_overlap}.")
            _log.warning("Recommend supplying renorm_args input.")
   
    elif model.lower() in ['bosz', 'ck04models', 'phoenix']:
        if sptype is None:
            _log.warning('sptype not specified. Using "flat".')
            sptype = 'flat'
            
        pl = {'sptype': sptype, 'Av': Av, 'renorm_args': renorm_args}
        sp = stellar_spectrum(sptype)
        if Av>0: 
            Rv = 4.0  
            sp *= s_ext.Extinction(Av/Rv, name='mwrv4')
        if (renorm_args is not None) and (len(renorm_args) > 0):
            sp_norm = sp.renorm(*renorm_args, force=True)
            sp = sp_norm
            
    name = kwargs.get('name')
    if name is not None:
        sp.name = name
        
    return sp

def bin_spectrum(sp, wave, waveunits='um'):
    """Rebin spectrum

    Rebin a synphot spectrum to a different wavelength grid.
    This function first converts the input spectrum to units
    of counts then combines the photon flux onto the
    specified wavelength grid.

    Output spectrum units are the same as the input spectrum.

    Parameters
    -----------
    sp : :class:`s_ext.Spectrum`
        Spectrum to rebin.
    wave : array_like
        Wavelength grid to rebin onto.
    waveunits : str
        Units of wave input. Must be recognizeable by synphot.

    Returns
    -------
    :class:`s_ext.Spectrum`
        Rebinned spectrum in same units as input spectrum.
    """

    from .maths import binned_statistic
    from synphot.binning import calculate_bin_edges

    waveunits0 = sp.waveunits
    fluxunits0 = sp.fluxunits

    # Convert wavelength of input spectrum to desired output units
    sp.convert(waveunits)
    # We also want input to be in terms of counts to conserve flux
    sp.convert('photlam')

    # Calculate bin edges
    edges = calculate_bin_edges(wave * sp.waveunits)
    ind = (sp.waveset >= edges[0]) & (sp.waveset <= edges[-1])
    binflux = binned_statistic(sp.wave[ind], sp.flux[ind], np.mean, bins=edges)

    # Interpolate over NaNs
    ind_nan = np.isnan(binflux)
    finterp = interp1d(wave[~ind_nan], binflux[~ind_nan], kind='cubic')
    binflux[ind_nan] = finterp(wave[ind_nan])

    sp2 = s_ext.ArraySpectrum(wave, binflux, waveunits=waveunits, fluxunits='photlam')
    sp2.convert(waveunits0)
    sp2.convert(fluxunits0)

    # Put back units of original input spectrum
    sp.convert(waveunits0)
    sp.convert(fluxunits0)

    return sp2

def mag_to_counts(src_mag, bandpass, sp_type='G0V', mag_units='vegamag', **kwargs):
        """
        Convert stellar magnitudes in some bandpass to corresponding flux values (e-/sec)
        """
        
        # Get flux of a 0 magnitude star (zero-point flux)
        sp = stellar_spectrum(sp_type, 0, mag_units, bandpass)
        obs = s_ext.Observation(sp, bandpass, binset=bandpass.wave)
        zp_counts = obs.effstim('counts') # Counts of a 0 mag star
        
        # Flux of each star e-/sec
        src_flux = np.array(zp_counts * 10**(-src_mag / 2.5))
        
        return src_flux

