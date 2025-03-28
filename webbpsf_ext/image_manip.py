from copy import deepcopy
from astropy.io.fits import hdu
import numpy as np
import multiprocessing as mp
import six

import scipy
from scipy import fftpack
from scipy.ndimage import fourier_shift, rotate

try:
    import cv2
    OPENCV_EXISTS = True
except ImportError:
    OPENCV_EXISTS = False

from astropy.convolution import Gaussian2DKernel
from astropy.io import fits

from poppy.utils import krebin

from .utils import siaf_nrc, siaf_mir, siaf_nis, siaf_fgs, siaf_nrs

# Program bar
from tqdm.auto import trange, tqdm

import logging
_log = logging.getLogger('webbpsf_ext')

###########################################################################
#    Image manipulation
###########################################################################

def get_im_cen(im):
    """
    Returns pixel position (xcen, ycen) of array center.
    For odd dimensions, this is in a pixel center.
    For even dimensions, this is at the pixel boundary.
    Assumes image size (ny, nx) is the last two dimensions of array.
    """
    if len(im.shape) > 1:
        ny, nx = im.shape[-2:]
    else:
        raise ValueError(f'Image dimension of {len(im.shape)} not valid.')
    
    return np.array([nx / 2. - 0.5, ny / 2. - 0.5])

def fshift(inarr, delx=0, dely=0, pad=False, cval=0.0, interp='linear', **kwargs):
    """ Fractional image shift
    
    Ported from IDL function fshift.pro.
    Routine to shift an image by non-integer values.

    Parameters
    ----------
    inarr: ndarray
        1D, or 2D array to be shifted. Can also be an image 
        cube assume with shape [nz,ny,nx].
    delx : float
        shift in x (same direction as IDL SHIFT function)
    dely: float
        shift in y
    pad : bool
        Should we pad the array before shifting, then truncate?
        Otherwise, the image is wrapped.
    cval : sequence or float, optional
        The values to set the padded values for each axis. Default is 0.
        ((before_1, after_1), ... (before_N, after_N)) unique pad constants for each axis.
        ((before, after),) yields same before and after constants for each axis.
        (constant,) or int is a shortcut for before = after = constant for all axes.
    interp : str
        Type of interpolation to use during the sub-pixel shift. Valid values are
        'linear', 'cubic', and 'quintic'.

        
    Returns
    -------
    ndarray
        Shifted image
    """
    
    from scipy.interpolate import interp1d, interp2d

    shape = inarr.shape
    ndim = len(shape)
    
    if ndim == 1:
        # Return if delx is 0
        if np.isclose(delx, 0, atol=1e-5):
            return inarr

        # separate shift into an integer and fraction shift
        intx = int(delx)
        fracx = delx - intx
        if fracx < 0:
            fracx += 1
            intx -= 1

        # Pad ends with constant value
        if pad:
            padx = np.abs(intx) + 5
            out = np.pad(inarr,np.abs(intx),'constant',constant_values=cval)
        else:
            padx = 0
            out = inarr.copy()

        # shift by integer portion
        out = np.roll(out, intx)
        # if significant fractional shift...
        if not np.isclose(fracx, 0, atol=1e-5):
            if interp=='linear':
                out = out * (1.-fracx) + np.roll(out,1) * fracx
            elif interp=='cubic':
                xvals = np.arange(len(out))
                fint = interp1d(xvals, out, kind=interp, bounds_error=False, fill_value='extrapolate')
                out = fint(xvals+fracx)
            elif interp=='quintic':
                xvals = np.arange(len(out))
                fint = interp1d(xvals, out, kind=5, bounds_error=False, fill_value='extrapolate')
                out = fint(xvals+fracx)
            else:
                raise ValueError(f'interp={interp} not recognized.')

        out = out[padx:padx+inarr.size]
    elif ndim == 2:	
        # Return if both delx and dely are 0
        if np.isclose(delx, 0, atol=1e-5) and np.isclose(dely, 0, atol=1e-5):
            return inarr

        ny, nx = shape

        # separate shift into an integer and fraction shift
        intx = int(delx)
        inty = int(dely)
        fracx = delx - intx
        fracy = dely - inty
        if fracx < 0:
            fracx += 1
            intx -= 1
        if fracy < 0:
            fracy += 1
            inty -= 1

        # Pad ends with constant value
        if pad:
            padx = np.abs(intx) + 5
            pady = np.abs(inty) + 5
            pad_vals = ([pady]*2,[padx]*2)
            out = np.pad(inarr,pad_vals,'constant',constant_values=cval)
        else:
            padx = 0; pady = 0
            out = inarr.copy()

        # shift by integer portion
        out = np.roll(out, (inty, intx), axis=(0,1))
    
        # Check if fracx and fracy are effectively 0
        fxis0 = np.isclose(fracx,0, atol=1e-5)
        fyis0 = np.isclose(fracy,0, atol=1e-5)
        
        if fxis0 and fyis0:
            # If fractional shifts are 0, no need for interpolation
            # Just perform whole pixel shifts
            pass
        elif interp=='linear':
            # Break bi-linear interpolation into four parts
            # to avoid NaNs unnecessarily affecting integer shifted dimensions
            part1 = out * ((1-fracx)*(1-fracy))
            part2 = 0 if fyis0 else np.roll(out,1,axis=0)*((1-fracx)*fracy)
            part3 = 0 if fxis0 else np.roll(out,1,axis=1)*((1-fracy)*fracx)
            part4 = 0 if (fxis0 or fyis0) else np.roll(np.roll(out, 1, axis=1), 1, axis=0) * fracx*fracy

            out = part1 + part2 + part3 + part4
        elif interp=='cubic' or interp=='quintic':
            fracx = 0 if fxis0 else fracx
            fracy = 0 if fxis0 else fracy
            
            y = np.arange(out.shape[0])
            x = np.arange(out.shape[1])
            fint = interp2d(x, y, out, kind=interp)
            out = fint(x-fracx, y-fracy)
        else:
            raise ValueError(f'interp={interp} not recognized.')
    
        out = out[pady:pady+ny, padx:padx+nx]
    elif ndim == 3:
        # Perform shift on each image in succession
        kwargs['delx'] = delx
        kwargs['dely'] = dely
        kwargs['pad'] = pad
        kwargs['cval'] = cval
        kwargs['interp'] = interp
        out = np.array([fshift(im, **kwargs) for im in inarr])

    else:
        raise ValueError(f'fshift: Found {ndim} dimensions {shape}. Only up to 3 dimensions allowed.')

    # Ensure the output isn't all NaNs
    if np.isnan(out).all():
        # Report number of NaNs in input and raise error 
        n_nan = np.sum(np.isnan(inarr))
        raise ValueError(f'fshift: All NaNs in final shifted array. Found {n_nan} NaNs in input.')

    return out

                          
def fourier_imshift(image, xshift, yshift, pad=False, cval=0.0, 
                    window_func=None, **kwargs):
    """Fourier shift image
    
    Shift an image by use of Fourier shift theorem

    Parameters
    ----------
    image : ndarray
        2D image or 3D image cube [nz,ny,nx].
    xshift : float
        Number of pixels to shift image in the x direction.
    yshift : float
        Number of pixels to shift image in the y direction.
    pad : bool
        Should we pad the array before shifting, then truncate?
        Otherwise, the image is wrapped.
    cval : sequence or float, optional
        The values to set the padded values for each axis. Default is 0.
        ((before_1, after_1), ... (before_N, after_N)) unique pad constants for each axis.
        ((before, after),) yields same before and after constants for each axis.
        (constant,) or int is a shortcut for before = after = constant for all axes.
    window_func : string, float, or tuple
        Name of window function from `scipy.signal.windows` to use before 
        Fourier shifting. The idea is to reduce artifacts from
        high frequency information during sub-pixel shifting. 
        This effectively acts as a low-pass filter applied
        to the Fourier transform of the image prior to shifting.
        Uses `skimage.filters.window()` to generate the window.
        For example:
            window_func='hann'
            window_func=('tukey', 0.25) # alpha=0.25
            window_func=('gaussian', 5) # std dev of 5 pixels
        Available options can be found: 
            https://docs.scipy.org/doc/scipy-1.12.0/reference/signal.windows.html

    Returns
    -------
    ndarray
        Shifted image
    """

    from skimage.filters import window as winfunc

    shape = image.shape
    ndim = len(shape)

    if ndim==2:

        ny, nx = shape
    
        # Pad ends with zeros
        if pad:
            padx = np.abs(int(xshift)) + 5
            pady = np.abs(int(yshift)) + 5
            pad_vals = ([pady]*2,[padx]*2)
            im = np.pad(image,pad_vals,'constant',constant_values=cval)
        else:
            padx = 0; pady = 0
            im = image
        
        im_fft = np.fft.fft2(im)
        if window_func is not None:
            im_otf = np.fft.fftshift(im_fft)
            im_otf *= winfunc(window_func, im_otf.shape)
            im_fft = np.fft.ifftshift(im_otf)
        offset = fourier_shift(im_fft, (yshift,xshift))
        offset = np.fft.ifft2(offset).real
        
        offset = offset[pady:pady+ny, padx:padx+nx]

        # Ensure the output isn't all NaNs
        if np.isnan(offset).all():
            # Report number of NaNs in input and raise error 
            n_nan = np.sum(np.isnan(image))
            raise ValueError(f'fourier_imshift: All NaNs in final shifted image. Found {n_nan} NaNs in input.')
        
    elif ndim==3:
        kwargs['pad'] = pad
        kwargs['cval'] = cval
        offset = np.array([fourier_imshift(im, xshift, yshift, **kwargs) for im in image])
    else:
        raise ValueError(f'fourier_imshift: Found {ndim} dimensions {shape}. Only up 2 or 3 dimensions allowed.')
    
    return offset
    
def cv_shift(image, xshift, yshift, pad=False, cval=0.0, interp='lanczos', **kwargs):
    """Use OpenCV library for image shifting

    Requires opencv-python package. Produces fewer artifacts that `fourier_imshift`.
    Faster than `fshift`.

    Parameters
    ----------
    image : ndarray
        2D image or 3D image cube [nz,ny,nx].
    xshift : float
        Number of pixels to shift image in the x direction
    yshift : float
        Number of pixels to shift image in the y direction
    pad : bool
        Should we pad the array before shifting, then truncate?
        Otherwise, the image is wrapped.
    cval : sequence or float, optional
        The values to set the padded values for each axis. Default is 0.
        ((before_1, after_1), ... (before_N, after_N)) unique pad constants for each axis.
        ((before, after),) yields same before and after constants for each axis.
        (constant,) or int is a shortcut for before = after = constant for all axes.
    interp : str
        Type of interpolation to use during the sub-pixel shift. Valid values are
        'linear', 'cubic', and 'lanczos'.

    Returns
    -------
    ndarray
        Shifted image
    """

    # If xshift and yshift are 0, then return the input image
    if np.isclose(xshift, 0, atol=1e-5) and np.isclose(yshift, 0, atol=1e-5):
        return image

    if OPENCV_EXISTS==False:
        raise ImportError('opencv-python not installed')

    shape = image.shape
    ndim = len(shape)

    if ndim==2:

        ny, nx = shape
    
        # Pad ends with zeros
        if pad:
            padx = np.abs(int(xshift)) + 5
            pady = np.abs(int(yshift)) + 5
            pad_vals = ([pady]*2,[padx]*2)
            im = np.pad(image,pad_vals,'constant',constant_values=cval)
        else:
            padx = 0; pady = 0
            im = image

        Mtrans = np.array([[1, 0, xshift], [0, 1, yshift]]).astype('float64')
        if interp=='linear':
            flags = cv2.INTER_LINEAR
        elif interp=='cubic':
            flags = cv2.INTER_CUBIC
        elif interp=='lanczos':
            flags = cv2.INTER_LANCZOS4
        else:
            raise ValueError(f"interp={interp} does not exist. Valid values are 'linear', 'cubic', or 'lanczos'.")

        offset = cv2.warpAffine(im, Mtrans, im.shape[::-1], flags=flags)
        offset = offset[pady:pady+ny, padx:padx+nx]

        # Ensure the output isn't all NaNs
        if np.isnan(offset).all():
            # Report number of NaNs in input and raise error 
            n_nan = np.sum(np.isnan(image))
            raise ValueError(f'cv_shift: All NaNs in final shifted image. Found {n_nan} NaNs in input.')

    elif ndim==3:
        kwargs = {'pad': pad, 'cval': cval, 'interp': interp}
        offset = np.array([cv_shift(im, xshift, yshift, **kwargs) for im in image])
    else:
        raise ValueError(f'cv_shift: Found {ndim} dimensions {shape}. Only up 2 or 3 dimensions allowed.')
    
    return offset

def fractional_image_shift(imarr, xshift, yshift, method='fourier', 
                           oversample=1, return_oversample=False, order=1,
                           gstd_pix=None, window_func=None, total=True, **kwargs):
    """Shift image(s) by a fractional amount

    Will first fix any NaNs using astropy convolution.
    
    Parameters
    ----------
    imarr : ndarray
        2D image or 3D image cube [nz,ny,nx].
    xshift : float
        Shift in x direction
    yshift : float
        Shift in y direction
    method : str
        Method to use for shifting. Options are:
        - 'fourier' : Shift in Fourier space
        - 'fshift' : Shift using interpolation
        - 'opencv' : Shift using OpenCV warpAffine

    oversample : int
        Factor to oversample the image before sub-pixel shifting. Default is 1.
        An oversample factor of 2 will increase the image size by 2x in each dimension.
    order : int
        The order of the spline interpolation for `zrebin` function, Default is 3. 
        Only used if oversample>1. If order=0, then `frebin` is used.
    gstd_pix : float
        Standard deviation of Gaussian kernel for smoothing. Default is None.
    return_oversample : bool
        Return the oversampled image after shifting. Default is False.
    window_func : string, float, or tuple
        Name of window function from `scipy.signal.windows` to use prior to
        shifting. The idea is to reduce artifacts from high frequency 
        information during sub-pixel shifting. This effectively acts as a 
        low-pass filter applied to the Fourier transform of the image prior 
        to shifting. Uses `skimage.filters.window()` to generate the window.
        Will apply to oversampled image, so make sure to adjust any function
        parameters accordingly.
        For example:
            .. code-block:: python
                window_func = 'hann'
                window_func = ('tukey', 0.25) # alpha=0.25
                window_func = ('gaussian', 5) # std dev of 5 pixels

        Available options can be found: 
            https://docs.scipy.org/doc/scipy-1.12.0/reference/signal.windows.html
    total : bool
        If True, then the total flux in the image is conserved. Default is True.
        Would want to set this to false if the image is in units of surface brightness
        (e.g., MJy/sr) and not flux (e.g., MJy).

    Keyword Args
    ------------
    pad : bool
        Pad the image before shifting, then truncate? Default is False.
    cval : sequence or float, optional
        The values to set the padded values for each axis. Default is 0.
        ((before_1, after_1), ... (before_N, after_N)) unique pad constants for each axis.
        ((before, after),) yields same before and after constants for each axis.
        (constant,) or int is a shortcut for before = after = constant for all axes.
    interp : str
        Type of interpolation to use during the sub-pixel shift. Valid values are
        'linear', 'cubic', and 'quintic' for `fshift` method (default: 'linear').
        For `opencv` method, valid values are 'linear', 'cubic', and 'lanczos' 
        (default: 'lanczos').
    """
    from astropy.convolution import Gaussian2DKernel, convolve

    # Replace NaNs with astropy convolved image values
    ind_nan_all = np.isnan(imarr)
    if ind_nan_all.any():
        kernel = Gaussian2DKernel(x_stddev=2)
        if len(imarr.shape)==3:
            imarr_conv = imarr.copy()
            im_mean = np.nanmean(imarr, axis=0)
            # First replace NaNs with mean of all images
            for i in range(imarr_conv.shape[0]):
                ind_nan = np.isnan(imarr[i])
                imarr_conv[i][ind_nan] = im_mean[ind_nan]
            # Use astropy convolve to fix remaining NaNs
            imarr_conv = np.array([convolve(im, kernel) for im in imarr_conv])
            for i in range(imarr.shape[0]):
                ind_nan = np.isnan(imarr[i])
                imarr[i][ind_nan] = imarr_conv[i][ind_nan]
        else:
            imarr_conv = convolve(imarr, kernel)
            ind_nan = np.isnan(imarr)
            imarr[ind_nan] = imarr_conv[ind_nan]

        del imarr_conv

    # Apply Gaussian smoothing (before rebinning if oversample<=1)
    if (gstd_pix is not None) and (gstd_pix>0) and (oversample<=1):
        gstd = gstd_pix
        kernel = Gaussian2DKernel(x_stddev=gstd)
        if len(imarr.shape)==3:
            imarr = np.array([image_convolution(im, kernel) for im in imarr])
        else:
            imarr = image_convolution(imarr, kernel)

        # print('gaussian:', imarr.shape, xsh, ysh, np.nansum(imarr))

    # Rebin pixels
    if oversample!=1:
        rescale_pix = kwargs.pop('rescale_pix', False)
        imarr = zrebin(imarr, oversample, order=order, 
                       total=total, rescale_pix=rescale_pix)
        xsh = xshift * oversample
        ysh = yshift * oversample
    else:
        xsh = xshift
        ysh = yshift

    # print('rebin:', imarr.shape, xsh, ysh, np.nansum(imarr))

    # Apply Gaussian smoothing (after rebinning)
    if (gstd_pix is not None) and (gstd_pix>0) and (oversample>1):
        gstd = gstd_pix * oversample
        kernel = Gaussian2DKernel(x_stddev=gstd)
        if len(imarr.shape)==3:
            imarr = np.array([image_convolution(im, kernel) for im in imarr])
        else:
            imarr = image_convolution(imarr, kernel)

        # print('gaussian:', imarr.shape, xsh, ysh, np.nansum(imarr))

    # Apply window function (low-pass filter)
    if (window_func is not None) and (method=='fourier'):
        kwargs['window_func'] = window_func
    elif window_func is not None:
        from skimage.filters import window as winfunc
        if len(imarr.shape)==3:
            for i, im in enumerate(imarr):
                im_otf = np.fft.fftshift(np.fft.fft2(im))
                im_otf *= winfunc(window_func, im_otf.shape)
                im = np.fft.ifft2(np.fft.ifftshift(im_otf)).real
                imarr[i] = im
        else:
            im_otf = np.fft.fftshift(np.fft.fft2(imarr))
            im_otf *= winfunc(window_func, im_otf.shape)
            imarr = np.fft.ifft2(np.fft.ifftshift(im_otf)).real

    # print(np.sum(np.isnan(imarr)), kwargs)

    # Shift the image
    if method=='fourier':
        imarr_shift = fourier_imshift(imarr, xsh, ysh, **kwargs)
    elif method=='fshift':
        imarr_shift = fshift(imarr, xsh, ysh, **kwargs)
    elif method=='opencv':
        imarr_shift = cv_shift(imarr, xsh, ysh, **kwargs)
    else:
        raise ValueError(f"Unrecognized method: {method}")

    # Add NaNs back to the image
    # if ind_nan_all.any():
    #     nan_mask_shift = fshift(ind_nan_all.astype('float'), xsh, ysh, pad=True, cval=1.0)
    #     imarr_shift[nan_mask_shift>0] = np.nan
    
    if return_oversample or oversample==1:
        # No need to resample back to original size
        return imarr_shift
    else:
        imarr_shift = frebin(imarr_shift, scale=1/oversample, total=total)
        # print('resample:', imarr_final.shape, np.nansum(imarr_final))
        return imarr_shift

def replace_nans_griddata(image, method='cubic', in_place=True, **kwargs):
    """Replace NaNs in an image using griddata interpolation
    
    Parameters
    ----------
    image : ndarray
        2D image [ny,nx].
    method : str
        Interpolation method to use for griddata. 
        Options are 'nearest', 'linear', or 'cubic'. Default is 'cubic'.
    in_place : bool
        Replace NaNs in place. Default is True.

    Keyword Args
    ------------
    fill_value : float, optional
        Value used to fill in for requested points outside of the convex hull of the input points. 
        If not provided, then the default is nan. This option has no effect for the 'nearest' method.
    rescale : bool, optional
        Rescale points to unit cube before performing interpolation. This is useful if some of the 
        input dimensions have incommensurable units and differ by many orders of magnitude.
    """

    from scipy.interpolate import griddata

    if not np.isnan(image).any():
        return image

    if not in_place:
        image = image.copy()

    xv = np.arange(image.shape[-1])
    yv = np.arange(image.shape[-2])
    xg, yg = np.meshgrid(xv, yv)
    ind_nan = np.isnan(image)

    fill_value = kwargs.get('fill_value', np.nan)
    rescale = kwargs.get('rescale', False)
    zgrid = griddata((xg[~ind_nan], yg[~ind_nan]), image[~ind_nan], 
                     (xg[ind_nan], yg[ind_nan]), method=method, 
                     fill_value=fill_value, rescale=rescale)

    image[ind_nan] = zgrid
    return image

def replace_nans(image, mean_func=np.nanmean, in_place=False,
                 use_griddata=True, grid_method='cubic', 
                 x_stddev=2, use_fft=False, **kwargs):
    """ Replace NaNs in an image with interpolated values

    If input is a cube, first replaces NaNs using mean of cube.

    Remaining NaNs are then replaced using griddata interpolation.
    Default is cubic interpolation.

    Any remaining NaNs are then replaced using astropy convolution.

    Parameters
    ----------
    image : ndarray
        2D image or 3D image cube [nz,ny,nx].
    mean_func : function
        Function to use for calculating the mean of the cube. Default is np.nanmean.
        Set this to None if you want to skip this step and only use griddata replacement.
    use_griddata : bool
        Use griddata interpolation to fix NaNs. Default is True.
    grid_method : str
        Interpolation method to use for griddata. 
        Options are 'nearest', 'linear', or 'cubic'. Default is 'cubic'.
    x_stddev : float
        Standard deviation of Gaussian kernel for smoothing. Default is 2.
    use_fft : bool
        Use FFT convolution. Default is False.

    Keyword Args
    ------------
    boundary : str, optional
        A flag indicating how to handle boundaries:
            * `None` : Set the ``result`` values to zero where the kernel
                extends beyond the edge of the array.
            * 'fill' : (default) Set values outside the array boundary to ``fill_value``.
            * 'wrap' : Periodic boundary that wrap to the other side of ``array``.
            * 'extend' : Set values outside the array to the nearest ``array``
                value.

    fill_value : float, optional
        The value to use outside the array when using ``boundary='fill'``.
    """

    from astropy.convolution import convolve, convolve_fft
    cfunc = convolve_fft if use_fft else convolve

    shape = image.shape
    ndim = len(shape)

    if ndim==3 and shape[0]==1:
        # If only one image in the cube, then just use 2D
        image = image[0]
        ndim = 2

    if not in_place:
        image = image.copy()

    # Replace NaNs with astropy convolved image values
    ind_nan_all = np.isnan(image)
    if ind_nan_all.any():
        kernel = Gaussian2DKernel(x_stddev=x_stddev)
        if ndim==3:
            if mean_func is not None:
                im_mean = mean_func(image, axis=0)
            # First replace NaNs with mean of all images
            for i in range(shape[0]):
                ind_nan_i = ind_nan_all[i]

                # First replace NaNs with mean of all images
                imfix = image[i].copy()
                if mean_func is not None:
                    imfix[ind_nan_i] = im_mean[ind_nan_i]

                # Recursively call this function to replace NaNs using griddata
                imfix = replace_nans(imfix, in_place=True,
                                     use_griddata=use_griddata, grid_method=grid_method, 
                                     x_stddev=x_stddev, use_fft=use_fft, **kwargs)

                # Replace NaNs with fixed values
                image[i][ind_nan_i] = imfix[ind_nan_i]               
        elif ndim==2:

            ind_nan = np.isnan(image)
            # print(ind_nan.sum())

            # Use scipy griddata to fix NaNs
            if use_griddata:
                image = replace_nans_griddata(image, method=grid_method, 
                                              in_place=True, **kwargs)
                ind_nan = np.isnan(image)
            else:
                ind_nan = ind_nan_all

            while ind_nan.any():
                # Use astropy convolve to fix remaining NaNs
                image[ind_nan] = cfunc(image, kernel, **kwargs)[ind_nan]
                ind_nan = np.isnan(image)

                # print(ind_nan.sum())
        else:
            raise ValueError(f'Found {ndim} dimensions {shape}. Only up 2 or 3 dimensions allowed.')

    return image.reshape(shape)

def image_shift_with_nans(image, xshift, yshift, shift_method='fourier', interp='linear',
                          gstd_pix=None, window_func=None, grid_method='cubic',
                          oversample=1, return_oversample=False, total=True, order=1,
                          pad=False, cval=np.nan, preserve_nans=False,
                          return_padded=False, **kwargs):
    """Shift image by a fractional amount accounting for NaNs
    
    Parameters
    ----------
    image : ndarray
        2D image or 3D image cube [nz,ny,nx].
    xshift : float
        Shift in x direction (pixels).
    yshift : float
        Shift in y direction (pixels).
    shift_method : str
        Method to use for shifting. Options are:
        - 'fourier' : Shift in Fourier space
        - 'fshift' : Shift using interpolation
        - 'opencv' : Shift using OpenCV warpAffine

    interp : str
        Type of interpolation to use during the sub-pixel shift. Valid values are:
        - 'fshift' : 'linear', 'cubic', and 'quintic' (default='linear')
        - `opencv` : 'linear', 'cubic', and 'lanczos' (default='lanczos')

    use_griddata : bool
        Use griddata interpolation to fix NaNs. Default is True.
    grid_method : str
        Interpolation method over NaNs to use for griddata. 
        Options are 'nearest', 'linear', or 'cubic'. Default is 'cubic'.
    oversample : int
        Factor to oversample the image before sub-pixel shifting. Default is 1.
        An oversample factor of 2 will increase the image size by 2x in each dimension.
    return_oversample : bool
        Return the oversampled image after shifting. Default is False.
    total : bool
        If True, then the total flux in the image is conserved. Default is True.
        Would want to set this to false if the image is in units of surface brightness
        (e.g., MJy/sr) and not flux (e.g., MJy).
    order : int
        The order of the spline interpolation for `zrebin` function, Default is 3. 
        Only used if oversample>1. If order=0, then `frebin` is used.
    pad : bool
        Pad the image before shifting, then truncate? Default is False.
    cval : sequence or float, optional
        The values to set the padded values for each axis. Default is NaN. 
        NaNs are then replaced with interpolated values during shifted and
        can be added back to the image using `preserve_nans`.
    preserve_nans : bool
        Add NaNs back to the image after shifting. Default is False.
    return_padded : bool
        Return the padded image after shifting. Default is False.
    gstd_pix : float
        Standard deviation of Gaussian kernel for smoothing. Default is None.
    window_func : string, float, or tuple
        Name of window function from `scipy.signal.windows` to use prior to
        shifting. The idea is to reduce artifacts from high frequency 
        information during sub-pixel shifting. This effectively acts as a 
        low-pass filter applied to the Fourier transform of the image prior 
        to shifting. Uses `skimage.filters.window()` to generate the window.
        Will apply to oversampled image, so make sure to adjust any function
        parameters accordingly.
        For example:
            .. code-block:: python
                window_func = 'hann'
                window_func = ('tukey', 0.25) # alpha=0.25
                window_func = ('gaussian', 5) # std dev of 5 pixels

    """


    from .maths import round_int

    ztol = 1e-5

    rescale_pix = kwargs.pop('rescale_pix', False)

    if return_padded:
        pad = True

    shape = image.shape
    ndim = len(shape)
    if ndim==2:
        ny, nx = shape
        nz = 1
        imarr = image.reshape([nz,ny,nx])
    elif ndim==3:
        nz, ny, nx = shape
        imarr = image
    else:
        raise ValueError(f'Found {ndim} dimensions {shape}. Only up 2 or 3 dimensions allowed.')
    
    # Pad image edges 
    # print('orig:', imarr.shape, np.nansum(imarr))
    if pad:
        padx = round_int(np.abs(xshift)) 
        pady = round_int(np.abs(yshift))
        new_shape = (ny+2*np.abs(pady), nx+2*np.abs(padx))
        imarr = crop_image(imarr, new_shape, fill_val=cval)
    else:
        padx = 0; pady = 0

    # print('padded:', imarr.shape, padx, pady, np.nansum(imarr))

    # Store image of NaNs and transform in same was as image
    if preserve_nans:
        imnans = np.isnan(imarr).astype('float')
        if oversample!=1:
            imnans = frebin(imnans, scale=oversample, total=False)
        # Shift NaN image
        xsh = xshift * oversample
        ysh = yshift * oversample
        imnans = fshift(imnans, xsh, ysh, pad=pad, cval=1)
        # Resample back to original size
        if not (return_oversample or oversample==1):
            imnans = frebin(imnans, scale=1/oversample, total=False)

    # Replace NaN with interpolated / extrapolated values
    imarr = replace_nans(imarr, in_place=False, grid_method=grid_method, **kwargs)
    # print('replace nans:', imarr.shape, np.nansum(imarr))

    # Perform rebinning, Gaussian smoothing, lowpass filtering, and image shift
    kwargs_sh = {
        'oversample': oversample, 'return_oversample': return_oversample,
        'method': shift_method, 'interp': interp, 
        'gstd_pix': gstd_pix, 'window_func': window_func, 
        'order': order, 'rescale_pix': rescale_pix,
        'total': total, 'pad': False, 'cval': 0, 
    }
    imarr_final = fractional_image_shift(imarr, xshift, yshift, **kwargs, **kwargs_sh)

    # print('shifted:', imarr_final.shape, np.nansum(imarr_final))

    # Add NaNs back to the image
    if preserve_nans:
        imarr_final[imnans>ztol] = np.nan

    if not return_padded:
        x1, x2 = (padx, padx+nx)
        y1, y2 = (pady, pady+ny)
        if return_oversample and oversample>1:
            x1, x2, y1, y2 = np.array([x1, x2, y1, y2]) * oversample
        imarr_final = imarr_final[:, y1:y2, x1:x2]
        # print('crop:', imarr_final.shape, (x1, x2), (y1, y2), np.nansum(imarr_final))

    if ndim==2:
        imarr_final = imarr_final[0]

    return imarr_final




###########################################################################
#    Image Cropping
###########################################################################

def pad_or_cut_to_size(array, new_shape, fill_val=0.0, offset_vals=None,
    shift_func=fshift, **kwargs):
    """
    Resize an array to a new shape by either padding with zeros
    or trimming off rows and/or columns. The output shape can
    be of any arbitrary amount.

    Parameters
    ----------
    array : ndarray
        A 1D, 2D, or 3D array. If 3D, then taken to be a stack of images
        that are cropped or expanded in the same fashion.
    new_shape : tuple
        Desired size for the output array. For 2D case, if a single value, 
        then will create a 2-element tuple of the same value.
    fill_val : scalar, optional
        Value to pad borders. Default is 0.0
    offset_vals : tuple
        Option to perform image shift in the (xpix) direction for 1D, 
        or (ypix,xpix) direction for 2D/3D prior to cropping or expansion.
    shift_func : function
        Function to use for shifting. Usually either `fshift` or `fourier_imshift`.
    interp : str
        Type of interpolation to use during the sub-pixel shift for `fshift`. 
        Valid values are 'linear', 'cubic', and 'quintic'.

    Returns
    -------
    output : ndarray
        An array of size new_shape that preserves the central information 
        of the input array.
    """
    
    shape_orig = array.shape
    ndim = len(shape_orig)
    if ndim == 1:
        # is_1d = True
        # Reshape array to a 2D array with nx=1
        array = array.reshape((1,1,-1))
        nz, ny, nx = array.shape
        if isinstance(new_shape, (float,int,np.int64)):
            nx_new = int(new_shape+0.5)
            ny_new = 1
        elif len(new_shape) < 2:
            nx_new = new_shape[0]
            ny_new = 1
        else:
            ny_new, nx_new = new_shape
        new_shape = (ny_new, nx_new)
        output = np.zeros(shape=(nz,ny_new,nx_new), dtype=array.dtype)
    elif (ndim == 2) or (ndim == 3):
        if ndim==2:
            nz = 1
            ny, nx = array.shape
            array = array.reshape([nz,ny,nx])
        else:
            nz, ny, nx = array.shape

        if isinstance(new_shape, (float,int,np.int64)):
            ny_new = nx_new = int(new_shape+0.5)
        elif len(new_shape) < 2:
            ny_new = nx_new = new_shape[0]
        else:
            ny_new, nx_new = new_shape
        new_shape = (ny_new, nx_new)
        output = np.zeros(shape=(nz,ny_new,nx_new), dtype=array.dtype)
    else:
        raise ValueError(f'Found {ndim} dimensions (shape={shape_orig}). Only up to 3 dimensions allowed.')
                      
    # Return if no difference in shapes
    # This needs to occur after the above so that new_shape is verified to be a tuple
    # If offset_vals is set, then continue to perform shift function
    if (shape_orig == new_shape) and (offset_vals is None):
        return array

    # Input the fill values
    if fill_val != 0:
        try:
            output += fill_val
        except:
            # If castings are different, then don't add fill_val
            pass
        
    # Pixel shift values
    if offset_vals is not None:
        if ndim == 1:
            ny_off = 0
            if isinstance(offset_vals, (float,int,np.int64)):
                nx_off = offset_vals
            elif len(offset_vals) < 2:
                nx_off = offset_vals[0]
            else:
                raise ValueError('offset_vals should be a single value.')
        else:
            if len(offset_vals) == 2:
                ny_off, nx_off = offset_vals
            else:
                raise ValueError('offset_vals should have two values.')
    else:
        nx_off = ny_off = 0
                
    if nx_new>nx:
        n0 = (nx_new - nx) / 2
        n1 = n0 + nx
    elif nx>nx_new:
        n0 = (nx - nx_new) / 2
        n1 = n0 + nx_new
    else:
        n0, n1 = (0, nx)
    n0 = int(n0+0.5)
    n1 = int(n1+0.5)

    if ny_new>ny:
        m0 = (ny_new - ny) / 2
        m1 = m0 + ny
    elif ny>ny_new:
        m0 = (ny - ny_new) / 2
        m1 = m0 + ny_new
    else:
        m0, m1 = (0, ny)
    m0 = int(m0+0.5)
    m1 = int(m1+0.5)

    if (nx_new>=nx) and (ny_new>=ny):
        #print('Case 1')
        output[:,m0:m1,n0:n1] = array.copy()
        for i, im in enumerate(output):
            output[i] = shift_func(im, nx_off, ny_off, pad=True, cval=fill_val, **kwargs)
    elif (nx_new<=nx) and (ny_new<=ny):
        #print('Case 2')
        if (nx_off!=0) or (ny_off!=0):
            array_temp = array.copy()
            for i, im in enumerate(array_temp):
                array_temp[i] = shift_func(im, nx_off, ny_off, pad=True, cval=fill_val, **kwargs)
            output = array_temp[:,m0:m1,n0:n1]
        else:
            output = array[:,m0:m1,n0:n1]
    elif (nx_new<=nx) and (ny_new>=ny):
        #print('Case 3')
        if nx_off!=0:
            array_temp = array.copy()
            for i, im in enumerate(array_temp):
                array_temp[i] = shift_func(im, nx_off, 0, pad=True, cval=fill_val, **kwargs)
            output[:,m0:m1,:] = array_temp[:,:,n0:n1]
        else:
            output[:,m0:m1,:] = array[:,:,n0:n1]
        for i, im in enumerate(output):
            output[i] = shift_func(im, 0, ny_off, pad=True, cval=fill_val, **kwargs)
    elif (nx_new>=nx) and (ny_new<=ny):
        #print('Case 4')
        if ny_off!=0:
            array_temp = array.copy()
            for i, im in enumerate(array_temp):
                array_temp[i] = shift_func(im, 0, ny_off, pad=True, cval=fill_val, **kwargs)
            output[:,:,n0:n1] = array_temp[:,m0:m1,:]
        else:
            output[:,:,n0:n1] = array[:,m0:m1,:]
        for i, im in enumerate(output):
            output[i] = shift_func(im, nx_off, 0, pad=True, cval=fill_val, **kwargs)
        
    # Flatten if input and output arrays are 1D
    if (ndim==1) and (ny_new==1):
        output = output.flatten()
    elif ndim==2:
        output = output[0]

    return output

def crop_observation(im_full, ap, xysub, xyloc=None, delx=0, dely=0, 
                     shift_func=fourier_imshift, interp='cubic',
                     return_xy=False, fill_val=np.nan, **kwargs):
    """Crop around aperture reference location

    `xysub` specifies the desired crop size.
    if `xysub` is an array, dimension order should be [nysub,nxsub].
    Crops at pixel boundaries (no interpolation) unless delx and dely
    are specified for pixel shifting.

    `xyloc` provides a way to manually supply the central position. 
    Set `ap` to None will crop around `xyloc` or center of array.

    delx and delx will shift array by some offset before cropping
    to allow for sub-pixel shifting. To change integer crop positions,
    recommend using `xyloc` instead.

    Shift function can be fourier_imshfit, fshift, or cv_shift.
    The interp keyword only works for the latter two options.
    Consider 'lanczos' for cv_shift.

    Setting `return_xy` to True will also return the indices 
    used to perform the crop.

    Parameters
    ----------
    im_full : ndarray
        Input image.
    ap : pysiaf aperture
        Aperture to use for cropping. Will crop around the aperture
        reference point by default. Will be overridden by `xyloc`.
    xysub : int, tuple, or list
        Size of subarray to extract. If a single integer is provided,
        then a square subarray is extracted. If a tuple or list is
        provided, then it should be of the form (ny, nx).
    xyloc : tuple or list
        (x,y) pixel location around which to crop the image. If None,
        then the image aperture refernece point is used.
    
    Keyword Args
    ------------
    delx : int or float
        Pixel offset in x-direction. This shifts the image by
        some number of pixels in the x-direction. Positive values shift
        the image to the right.
    dely : int or float
        Pixel offset in y-direction. This shifts the image by
        some number of pixels in the y-direction. Positive values shift
        the image up.
    shift_func : function
        Function to use for shifting. Default is `fourier_imshift`.
        If delx and dely are both integers, then `fshift` is used.
    interp : str
        Interpolation method to use for shifting. Default is 'cubic'.
        Options are 'nearest', 'linear', 'cubic', and 'quadratic'
        for `fshift`.
    return_xy : bool
        If True, then return the x and y indices used to crop the
        image prior to any shifting from `delx` and `dely`; 
        (x1, x2, y1, y2). Default is False.
    fill_val : float
        Value to use for filling in the empty pixels after shifting.
        Default = np.nan.
    """
        
    from .maths import round_int

    # xcorn_sci, ycorn_sci = ap.corners('sci')
    # xcmin, ycmin = (int(xcorn_sci.min()+0.5), int(ycorn_sci.min()+0.5))
    # xsci_arr = np.arange(1, im_full.shape[1]+1)
    # ysci_arr = np.arange(1, im_full.shape[0]+1)

    
    # Cut out postage stamp from full frame image
    if isinstance(xysub, (list, tuple, np.ndarray)):
        ny_sub, nx_sub = xysub
    else:
        ny_sub = nx_sub = xysub
    
    # Get centroid position
    if ap is None:
        xc, yc = get_im_cen(im_full) if xyloc is None else xyloc
    else: 
        # Subtract 1 from sci coords to get indices
        xc, yc = (ap.XSciRef-1, ap.YSciRef-1) if xyloc is None else xyloc

    x1 = round_int(xc - nx_sub/2 + 0.5)
    x2 = x1 + nx_sub
    y1 = round_int(yc - ny_sub/2 + 0.5)
    y2 = y1 + ny_sub

    # Save initial values in case they get modified below
    x1_init, x2_init = (x1, x2)
    y1_init, y2_init = (y1, y2)
    xy_ind = np.array([x1_init, x2_init, y1_init, y2_init])

    sh_orig = im_full.shape
    if (x2>=sh_orig[1]) or (y2>=sh_orig[0]) or (x1<0) or (y1<0):
        ny, nx = sh_orig

        # Get expansion size along x-axis
        dxp = x2 - nx + 1
        dxp = 0 if dxp<0 else dxp
        dxn = -1*x1 if x1<0 else 0
        dx = dxp + dxn

        # Get expansion size along y-axis
        dyp = y2 - ny + 1
        dyp = 0 if dyp<0 else dyp
        dyn = -1*y1 if y1<0 else 0
        dy = dyp + dyn

        # Expand image
        # TODO: This can probelmatic for some existing functions because it
        # places NaNs in the output image.
        shape_new = (2*dy+ny, 2*dx+nx)
        im_full = pad_or_cut_to_size(im_full, shape_new, fill_val=fill_val)

        xc_new, yc_new = (xc+dx, yc+dy)
        x1 = round_int(xc_new - nx_sub/2 + 0.5)
        x2 = x1 + nx_sub
        y1 = round_int(yc_new - ny_sub/2 + 0.5)
        y2 = y1 + ny_sub
    # else:
    #     xc_new, yc_new = (xc, yc)
    #     shape_new = sh_orig

    # if (x1<0) or (y1<0):
    #     dx = -1*x1 if x1<0 else 0
    #     dy = -1*y1 if y1<0 else 0

    #     # Expand image
    #     shape_new = (2*dy+shape_new[0], 2*dx+shape_new[1])
    #     im_full = pad_or_cut_to_size(im_full, shape_new)

    #     xc_new, yc_new = (xc_new+dx, yc_new+dy)
    #     x1 = round_int(xc_new - nx_sub/2 + 0.5)
    #     x2 = x1 + nx_sub
    #     y1 = round_int(yc_new - ny_sub/2 + 0.5)
    #     y2 = y1 + ny_sub

    # Perform pixel shifting
    if delx!=0 or dely!=0:
        kwargs['interp'] = interp
        # Use fshift function if only performing integer shifts
        # if float(delx).is_integer() and float(dely).is_integer():
        #     shift_func = fshift

        # If NaNs are present, print warning and fill with zeros
        ind_nan = np.isnan(im_full)
        if np.any(ind_nan) and (shift_func is not image_shift_with_nans):
            # _log.warning('NaNs present in image. Filling with zeros.')
            im_full = im_full.copy()
            im_full[ind_nan] = 0

        kwargs['pad'] = True
        im_full = shift_func(im_full, delx, dely, **kwargs)
        # shift NaNs and add back in
        if np.any(ind_nan):
            ind_nan = fshift(ind_nan, delx, dely, pad=True) > 0  # Maybe >0.5?
            im_full[ind_nan] = np.nan
    
    im = im_full[y1:y2, x1:x2]
    
    if return_xy:
        return im, xy_ind
    else:
        return im


def crop_image(imarr, xysub, xyloc=None, **kwargs):
    """Crop input image around center using integer offsets only

    If size is exceeded, then the image is expanded and filled with NaNs.

    Parameters
    ----------
    imarr : ndarray
        Input image or image cube [nz,ny,nx].
    xysub : int, tuple, or list
        Size of subarray to extract. If a single integer is provided,
        then a square subarray is extracted. If a tuple or list is
        provided, then it should be of the form (ny, nx).
    xyloc : tuple or list
        (x,y) pixel location around which to crop the image. If None,
        then the image center is used.
    
    Keyword Args
    ------------
    delx : int or float
        Integer pixel offset in x-direction. This shifts the image by
        some number of pixels in the x-direction. Positive values shift
        the image to the right.
    dely : int or float
        Integer pixel offset in y-direction. This shifts the image by
        some number of pixels in the y-direction. Positive values shift
        the image up.
    shift_func : function
        Function to use for shifting. Default is `fourier_imshift`.
        If delx and dely are both integers, then `fshift` is used.
    interp : str
        Interpolation method to use for shifting. Default is 'cubic'.
        Options are 'nearest', 'linear', 'cubic', and 'quadratic'
        for `fshift`.
    return_xy : bool
        If True, then return the x and y indices used to crop the
        image prior to any shifting from `delx` and `dely`; 
        (x1, x2, y1, y2). Default is False.
    fill_val : float
        Value to use for filling in the empty pixels after shifting.
        Default = np.nan.
    """
    
    sh = imarr.shape
    if len(sh) == 2:
        return crop_observation(imarr, None, xysub, xyloc=xyloc, **kwargs)
    elif len(sh) == 3:
        return_xy = kwargs.pop('return_xy', False)
        res = np.asarray([crop_observation(im, None, xysub, xyloc=xyloc, **kwargs) for im in imarr])
        if return_xy:
            _, xy = crop_observation(imarr[0], None, xysub, xyloc=xyloc, return_xy=True, **kwargs)
            return (res, xy)
        else:
            return res 
    else:
        raise ValueError(f'Found {len(sh)} dimensions {sh}. Only 2 or 3 dimensions allowed.')


def rotate_offset(data, angle, cen=None, cval=0.0, order=1, 
    reshape=True, recenter=True, shift_func=fshift, **kwargs):
    """Rotate and offset an array.

    Same as `rotate` in `scipy.ndimage` except that it
    rotates around a center point given by `cen` keyword.
    The array is rotated in the plane defined by the two axes given by the
    `axes` parameter using spline interpolation of the requested order.
    Default rotation is clockwise direction.
    
    Parameters
    ----------
    data : ndarray
        The input array.
    angle : float
        The rotation angle in degrees (rotates in clockwise direction).
    cen : tuple
        Center location around which to rotate image.
        Values are expected to be `(xcen, ycen)`.
    recenter : bool
        Do we want to reposition so that `cen` is the image center?
    shift_func : function
        Function to use for shifting. Usually either `fshift` or `fourier_imshift`.
        
    Keyword Args
    ------------
    axes : tuple of 2 ints, optional
        The two axes that define the plane of rotation. Default is the first
        two axes.
    reshape : bool, optional
        If `reshape` is True, the output shape is adapted so that the input
        array is contained completely in the output. The `cen` coordinate
        is now the center of the array. Default is True.
    order : int, optional
        The order of the spline interpolation, default is 1.
        The order has to be in the range 0-5.
    mode : str, optional
        Points outside the boundaries of the input are filled according
        to the given mode ('constant', 'nearest', 'reflect', 'mirror' or 'wrap').
        Default is 'constant'.
    cval : scalar, optional
        Value used for points outside the boundaries of the input if
        ``mode='constant'``. Default is 0.0
    prefilter : bool, optional
        The parameter prefilter determines if the input is pre-filtered with
        `spline_filter` before interpolation (necessary for spline
        interpolation of order > 1).  If False, it is assumed that the input is
        already filtered. Default is True.

    Returns
    -------
    rotate : ndarray or None
        The rotated data.

    """

    # Return input data if angle is set to None or 0
    # and if 
    if ((angle is None) or (angle==0)) and (cen is None):
        return data

    shape_orig = data.shape
    ndim = len(shape_orig)
    if ndim==2:
        ny, nx = shape_orig
        nz = 1
    elif ndim==3:
        nz, ny, nx = shape_orig
    else:
        raise ValueError(f'Found {ndim} dimensions {shape_orig}. Only 2 or 3 dimensions allowed.')    

    if 'axes' not in kwargs.keys():
        kwargs['axes'] = (2,1)
    kwargs['order'] = order
    kwargs['cval'] = cval

    # xcen, ycen = (nx/2, ny/2)
    xcen, ycen = get_im_cen(data)
    if cen is None:
        cen = (xcen, ycen)
    xcen_new, ycen_new = cen
    delx, dely = (xcen-xcen_new, ycen-ycen_new)

    # Reshape into a 3D array if nz=1
    data = data.reshape([nz,ny,nx])
    # Return rotate function if rotating about center
    if np.allclose((delx, dely), 0, atol=1e-5):
        return rotate(data, angle, reshape=reshape, **kwargs).squeeze()

    # fshift interp type
    if order <=1:
        interp='linear'
    elif order <=3:
        interp='cubic'
    else:
        interp='quintic'

    # Pad and then shift array
    # Places `cen` position at center of image
    new_shape = (int(ny+2*abs(dely)), int(nx+2*abs(delx)))
    images_shift = []
    for im in data:
        # im_pad = pad_or_cut_to_size(im, new_shape, fill_val=cval)
        im_pad = crop_image(im, new_shape, fill_val=cval)
        im_new = shift_func(im_pad, delx, dely, cval=cval, interp=interp)
        images_shift.append(im_new)
    images_shift = np.asarray(images_shift)
    
    if reshape:
        # Rotate around current center and expand to full size
        images_fin = rotate(images_shift, angle, reshape=True, **kwargs)
    else:
        # Perform cropping
        if recenter:
            # Keeping 'cen' position in center; no need to reshape to larger size
            images_rot = rotate(images_shift, angle, reshape=False, **kwargs)
        else:
            # Reshape to larger size due to image shifting
            images_shrot = rotate(images_shift, angle, reshape=True, **kwargs)
            images_rot = []
            # Shift 'cen' back to original location
            for im in images_shrot:
                im_new = shift_func(im, -1*delx, -1*dely, pad=True, cval=cval, interp=interp)
                images_rot.append(im_new)
            images_rot = np.asarray(images_rot)
    
        # Perform cropping
        images_fin = []
        for im in images_rot:
            # im_new = pad_or_cut_to_size(im, (ny,nx))
            im_new = crop_image(im, (ny,nx), fill_val=0)
            images_fin.append(im_new)
        images_fin = np.asarray(images_fin)
    
    # Drop out single-valued dimensions
    return images_fin.squeeze()

def zrebin(image, oversample, order=3, mode='reflect', total=True, 
           rescale_pix=False, **kwargs):
    """Rebin image using scipy.ndimage.zoom
    
    Parameters
    ----------
    image : ndarray
        Input image
    oversample : float
        Factor to scale output array size. A scale of 2 will increase
        the number of pixels by 2 (ie., finer pixel scale). If less than
        1, then will decrease the number of pixels using `frebin`; no
        interpolation is performed.
    order : int
        The order of the spline interpolation, default is 3. Only used
        if oversample>1. If order=0, then `frebin` is used.
    mode : str
        Points outside the boundaries of the input are filled according
        to the given mode ('constant', 'nearest', 'reflect', 'mirror' or 'wrap').
        Default is 'reflect'. Only used if oversample>1.
    total : bool
        Conserves the surface flux. If True, the output pixels 
        will be the sum of pixels within the appropriate box of 
        the input image. Otherwise, they will be the average.
    """


    import scipy.ndimage
    def dtype_check(result, input_dtype):
        """Check if resultis same as input dtype
        
        If total is True, then prints a warning, otherwise 
        changes back to input dtype.
        """
        # Because we're preserving total, may be unable to preserve input dtype
        if result.dtype != input_dtype:
            if total:
                _log.warning(f'dtype was updated from {input_dtype} to {result.dtype}')
            else:
                result = result.astype(input_dtype)
        return result

    # Check if dtype is preserved
    input_dtype = image.dtype

    shape = image.shape
    ndim = len(shape)

    if ndim==2:
        if oversample==1:
            return image
        elif (oversample<1) or (order is None) or (order==0):
            return frebin(image, scale=oversample, total=total)
        else:
            # Result has a sum that is ~oversample**2 times the input
            result = scipy.ndimage.zoom(image, oversample, order=order, mode=mode, **kwargs)

            # Zoom does not preserve flux within a set of oversampled pixels
            # Ensure pixel values are conserved if oversample>1
            if rescale_pix:
                res_reshape = result.reshape((shape[0],oversample,shape[1],oversample))
                res_trans = res_reshape.transpose(1,3,0,2).reshape([-1,shape[0],shape[1]])
                res_resum = res_trans.sum(axis=0)
                res_scale = image / res_resum
                res_trans_new = res_trans * res_scale
                res_reshape_new = res_trans_new.reshape([oversample,oversample,shape[0],shape[1]])
                result_new = res_reshape_new.transpose(2,0,3,1).reshape(result.shape)
                result = result_new.copy()
                del result_new, res_reshape_new, res_trans_new, res_scale, res_resum, res_trans, res_reshape

                if not total:
                    result *= oversample**2.
            elif total: 
                result /= oversample**2.

            return dtype_check(result, input_dtype)
            
    elif ndim==3:
        kwargs = {
            'order': order, 
            'mode': mode, 
            'total': total, 
            'rescale_pix': rescale_pix,
        }
        result = np.array([zrebin(im, oversample, **kwargs) for im in image])
        return result        

def frebin(image, dimensions=None, scale=None, total=True):
    """Fractional rebin
    
    Python port from the IDL frebin.pro
    Shrink or expand the size of a 1D or 2D array by an arbitary amount 
    using bilinear interpolation. Conserves flux by ensuring that each 
    input pixel is equally represented in the output array. Can also input
    an image cube.

    Parameters
    ----------
    image : ndarray
        Input image ndarray (1D, 2D). Can also be an image 
        cube assumed to have shape [nz,ny,nx].
    dimensions : tuple or None
        Desired size of output array (take priority over scale).
    scale : tuple or None
        Factor to scale output array size. A scale of 2 will increase
        the number of pixels by 2 (ie., finer pixel scale).
    total : bool
        Conserves the surface flux. If True, the output pixels 
        will be the sum of pixels within the appropriate box of 
        the input image. Otherwise, they will be the average.
    
    Returns
    -------
    ndarray
        The binned ndarray
    """

    def dtype_check(result, input_dtype):
        """Check if resultis same as input dtype
        
        If total is True, then prints a warning, otherwise 
        changes back to input dtype.
        """
        # Because we're preserving total, may be unable to preserve input dtype
        if result.dtype != input_dtype:
            if total:
                _log.warning(f'dtype was updated from {input_dtype} to {result.dtype}')
            else:
                result = result.astype(input_dtype)
        return result

    shape = image.shape
    ndim = len(shape)
    if ndim>2:
        ndim_temp = 2
        sh_temp = shape[-2:]
    else:
        ndim_temp = ndim
        sh_temp = shape

    if dimensions is not None:
        if isinstance(dimensions, float):
            dimensions = [int(dimensions)] * ndim_temp
        elif isinstance(dimensions, int):
            dimensions = [dimensions] * ndim_temp
        elif len(dimensions) != ndim_temp:
            raise RuntimeError("The number of input dimensions don't match the image shape.")
    elif scale is not None:
        if isinstance(scale, float) or isinstance(scale, int):
            dimensions = list(map(int, map(lambda x: x+0.5, map(lambda x: x*scale, sh_temp))))
        elif len(scale) != ndim_temp:
            raise RuntimeError("The number of input dimensions don't match the image shape.")
        else:
            dimensions = [scale[i]*sh_temp[i] for i in range(len(scale))]
    else:
        raise RuntimeError('Incorrect parameters to rebin.\n\frebin(image, dimensions=(x,y))\n\frebin(image, scale=a')
    #print(dimensions)

    # Check if dtype is preserved
    input_dtype = image.dtype

    if ndim==1:
        nlout = 1
        nsout = dimensions[0]
        nsout = int(nsout+0.5)
        dimensions = [nsout]
    elif ndim==2:
        nlout, nsout = dimensions
        nlout = int(nlout+0.5)
        nsout = int(nsout+0.5)
        dimensions = [nlout, nsout]
    elif ndim==3:
        kwargs = {'dimensions': dimensions, 'scale': scale, 'total': total}
        result = np.array([frebin(im, **kwargs) for im in image])
        return result
    elif ndim > 3:
        raise ValueError(f'Found {ndim} dimensions {shape}. Only up to 3 dimensions allowed.')    

    if nlout != 1:
        nl = shape[0]
        ns = shape[1]
    else:
        nl = nlout
        ns = shape[0]

    sbox = ns / float(nsout)
    lbox = nl / float(nlout)
    #print(sbox,lbox)

    # Contract by integer amount
    if (sbox.is_integer()) and (lbox.is_integer()):
        image = image.reshape((nl,ns))
        result = krebin(image, (nlout,nsout))
        if not total: 
            result = result / (sbox*lbox)

        result = dtype_check(result, input_dtype)
        if nl == 1:
            return result[0,:]
        else:
            return result

    ns1 = ns - 1
    nl1 = nl - 1

    if nl == 1:
        #1D case
        _log.debug("Rebinning to Dimension: %s" % nsout)
        result = np.zeros(nsout)
        for i in range(nsout):
            rstart = i * sbox
            istart = int(rstart)
            rstop = rstart + sbox

            if int(rstop) < ns1:
                istop = int(rstop)
            else:
                istop = ns1

            frac1 = float(rstart) - istart
            frac2 = 1.0 - (rstop - istop)

            #add pixel values from istart to istop and subtract fraction pixel
            #from istart to rstart and fraction pixel from rstop to istop
            result[i] = np.sum(image[istart:istop + 1]) - frac1 * image[istart] - frac2 * image[istop]

        if not total:
            result = result / (float(sbox) * lbox)
        return dtype_check(result, input_dtype)

    else:
        _log.debug("Rebinning to Dimensions: %s, %s" % tuple(dimensions))
        #2D case, first bin in second dimension
        temp = np.zeros((nlout, ns))
        result = np.zeros((nsout, nlout))

        if (result.dtype != input_dtype) and ('float' in input_dtype.name) and ('float' in result.dtype.name):
            result = result.astype(input_dtype)
            temp = temp.astype(input_dtype)

        #first lines
        for i in range(nlout):
            rstart = i * lbox
            istart = int(rstart)
            rstop = rstart + lbox

            if int(rstop) < nl1:
                istop = int(rstop)
            else:
                istop = nl1

            frac1 = float(rstart) - istart
            frac2 = 1.0 - (rstop - istop)

            if istart == istop:
                temp[i, :] = (1.0 - frac1 - frac2) * image[istart, :]
            else:
                temp[i, :] = np.sum(image[istart:istop + 1, :], axis=0) -\
                             frac1 * image[istart, :] - frac2 * image[istop, :]

        temp = temp.T

        #then samples
        for i in range(nsout):
            rstart = i * sbox
            istart = int(rstart)
            rstop = rstart + sbox

            if int(rstop) < ns1:
                istop = int(rstop)
            else:
                istop = ns1

            frac1 = float(rstart) - istart
            frac2 = 1.0 - (rstop - istop)

            if istart == istop:
                result[i, :] = (1. - frac1 - frac2) * temp[istart, :]
            else:
                result[i, :] = np.sum(temp[istart:istop + 1, :], axis=0) - \
                                      frac1 * temp[istart, :] - frac2 * temp[istop, :]

        result = result.T

        if not total:
            result = result / (float(sbox) * lbox)
        return dtype_check(result, input_dtype)


def image_rescale(HDUlist_or_filename, pixscale_out, pixscale_in=None, 
                  dist_in=None, dist_out=None, cen_star=True, shape_out=None):
    """ Rescale image flux

    Scale the flux and rebin an image to some new pixel scale and distance. 
    The object's physical units (AU) are assumed to be constant, so the 
    total angular size changes if the distance to the object changes.

    IT IS RECOMMENDED THAT UNITS BE IN PHOTONS/SEC/PIXEL (not mJy/arcsec)

    Parameters
    ==========
    HDUlist_or_filename : HDUList or str
        Input either an HDUList or file name.
    pixscale_out : float
        Desired pixel scale (asec/pix) of returned image. Will be saved in header info.
    
    Keyword Args
    ============
    pixscale_in : float or None
        Input image pixel scale. If None, then tries to grab info from the header.
    dist_in : float
        Input distance (parsec) of original object. If not set, then we look for 
        the header keywords 'DISTANCE' or 'DIST'.
    dist_out : float
        Output distance (parsec) of object in image. Will be saved in header info.
        If not set, then assumed to be same as input distance.
    cen_star : bool
        Is the star placed in the central pixel? If so, then the stellar flux is 
        assumed to be a single pixel that is equal to the maximum flux in the
        image. Rather than rebinning that pixel, the total flux is pulled out
        and re-added to the central pixel of the final image.
    shape_out : tuple, int, or None
        Desired size for the output array (ny,nx). If a single value, then will 
        create a 2-element tuple of the same value.

    Returns
    =======
        HDUlist of the new image.
    """

    if isinstance(HDUlist_or_filename, six.string_types):
        hdulist = fits.open(HDUlist_or_filename)
    elif isinstance(HDUlist_or_filename, fits.HDUList):
        hdulist = HDUlist_or_filename
    else:
        raise ValueError("Input must be a filename or HDUlist")
    
    header = hdulist[0].header
    # Try to update input pixel scale if it exists in header
    if pixscale_in is None:
        key_test = ['PIXELSCL','PIXSCALE']
        for k in key_test:
            if k in header:
                pixscale_in = header[k]
        if pixscale_in is None:
            raise KeyError("Cannot determine input image pixel scale.")

    # Try to update input distance if it exists in header
    if dist_in is None:
        key_test = ['DISTANCE','DIST']
        for k in key_test:
            if k in header:
                dist_in = header[k]

    # If output distance is not set, set to input distance
    if dist_out is None:
        dist_out = 'None' if dist_in is None else dist_in
        fratio = 1
    elif dist_in is None:
        raise ValueError('Input distance should not be None if output distance is specified.')
    else:
        fratio = dist_in / dist_out

    # Scale the input flux by inverse square law
    image = (hdulist[0].data) * fratio**2

    # If we move the image closer while assuming same number of pixels with
    # the same AU/pixel, then this implies we've increased the angle that 
    # the image subtends. So, each pixel would have a larger angular size.
    # New image scale in arcsec/pixel
    imscale_new = pixscale_in * fratio

    # Before rebinning, we want the flux in the central pixel to
    # always be in the central pixel (the star). So, let's save
    # and remove that flux then add back after the rebinning.
    if cen_star:
        mask_max = image==image.max()
        star_flux = image[mask_max][0]
        image[mask_max] = 0

    # Rebin the image to get a pixel scale that oversamples the detector pixels
    fact = imscale_new / pixscale_out
    image_new = frebin(image, scale=fact)

    # Restore stellar flux to the central pixel.
    ny, nx = image_new.shape
    if cen_star:
        image_new[ny//2, nx//2] += star_flux

    if shape_out is not None:
        # image_new = pad_or_cut_to_size(image_new, shape_out)
        image_new = crop_image(image_new, shape_out, fill_val=0)

    hdu_new = fits.PrimaryHDU(image_new)
    hdu_new.header = hdulist[0].header.copy()
    hdulist_new = fits.HDUList([hdu_new])
    hdulist_new[0].header['PIXELSCL'] = (pixscale_out, 'arcsec/pixel')
    hdulist_new[0].header['PIXSCALE'] = (pixscale_out, 'arcsec/pixel')
    hdulist_new[0].header['DISTANCE'] = (dist_out, 'parsecs')

    return hdulist_new


def model_to_hdulist(args_model, sp_star, bandpass):

    """HDUList from model FITS file.

    Convert disk model to an HDUList with units of photons/sec/pixel.
    If observed filter is different than input filter, we assume that
    the disk has a flat scattering, meaning it scales with stellar
    continuum. Pixel sizes and distances are left unchanged, and
    stored in header.

    Parameters
    ----------
    args_model - tuple
        Arguments describing the necessary model information:
            - fname   : Name of model file or an HDUList
            - scale0  : Pixel scale (in arcsec/pixel)
            - dist0   : Assumed model distance
            - wave_um : Wavelength of observation
            - units0  : Assumed flux units (e.g., MJy/arcsec^2 or muJy/pixel)
    sp_star : :class:`webbpsf_ext.synphot_ext.Spectrum`
        A synphot spectrum of central star. Used to adjust observed
        photon flux if filter differs from model input
    bandpass : :mod:`webbpsf_ext.synphot_ext.Bandpass`
        Output synphot bandpass from instrument class. This corresponds 
        to the flux at the entrance pupil for the particular filter.
    """

    from .synphot_ext import stsyn, validate_unit
    from synphot.units import convert_flux, validate_wave_unit
    import astropy.units as u

    #filt, mask, pupil = args_inst
    fname, scale0, dist0, wave_um, units0 = args_model
    wave0 = wave_um * 1e4

    #### Read in the image, then convert from mJy/arcsec^2 to photons/sec/pixel

    if isinstance(fname, fits.HDUList):
        hdulist = fname
    else:
        # Open file
        hdulist = fits.open(fname)

    # Get rid of any non-standard header keywords
    hdu = fits.PrimaryHDU(hdulist[0].data)
    for k in hdulist[0].header.keys():
        try:
            hdu.header[k] = hdulist[0].header[k]
        except ValueError:
            pass
    hdulist = fits.HDUList(hdu)

    # Break apart units0
    units_list = units0.split('/')
    unit_type = validate_unit(units_list[0])
    im = convert_flux(wave0, hdulist[0].data*unit_type, 'photlam',
                      area=stsyn.conf.area, vegaspec=stsyn.Vega)
    im = im.value

    # We assume scattering is flat in photons/sec/A
    # This means everything scales with stellar continuum
    sp_star.convert('photlam')
    wstar, fstar = (sp_star.wave/1e4, sp_star.flux)

    # Compare observed wavelength to image wavelength
    wobs_um = bandpass.avgwave().to_value('um') # Current bandpass wavelength

    wdel = np.linspace(-0.1,0.1)
    f_obs = np.interp(wobs_um+wdel, wstar, fstar)
    f0    = np.interp(wave_um+wdel, wstar, fstar)
    im *= np.mean(f_obs / f0)

    # Convert to photons/sec/pixel
    im *= bandpass.equivwidth().to_value(u.AA) * stsyn.conf.area
    # If input units are per arcsec^2 then scale by pixel scale
    # This will give ph/sec for each pixel
    if ('arcsec' in units_list[1]) or ('asec' in units_list[1]):
        im *= scale0**2
    elif 'mas' in units_list[1]:
        im *= (scale0*1000)**2
    elif 'sr' in units_list[1].lower():
        # Steradians to arcsec^2
        sr_to_asec2 = (3600*180/np.pi)**2 # [asec^2 / sr]
        im *= (scale0**2 / sr_to_asec2) 

    # Save into HDUList
    hdulist[0].data = im

    hdulist[0].header['UNITS']    = 'photons/sec'
    hdulist[0].header['PIXELSCL'] = (scale0, 'arcsec/pixel')
    hdulist[0].header['PIXSCALE'] = (scale0, 'arcsec/pixel') # Alternate keyword
    hdulist[0].header['DISTANCE'] = (dist0, 'parsecs')

    return hdulist


def distort_image(hdulist_or_filename, ext=0, to_frame='sci', fill_value=0, 
                  xnew_coords=None, ynew_coords=None, return_coords=False,
                  aper=None, sci_cen=None, pixelscale=None, oversamp=None):
    """ Distort an image

    Apply SIAF instrument distortion to an image that is assumed to be in 
    its ideal coordinates. The header information should contain the relevant
    SIAF point information, such as SI instrument, aperture name, pixel scale,
    detector oversampling, and detector position ('sci' coords).

    This function then transforms the image to the new coordinate system using
    scipy's RegularGridInterpolator (linear interpolation).

    Parameters
    ----------
    hdulist_or_filename : str or HDUList
        A PSF from STPSF, either as an HDUlist object or as a filename
    ext : int
        Extension of HDUList to perform distortion on.
    fill_value : float or None
        Value used to fill in any blank space by the skewed PSF. Default = 0.
        If set to None, values outside the domain are extrapolated.
    to_frame : str
        Type of input coordinates. 

            * 'tel': arcsecs V2,V3
            * 'sci': pixels, in conventional DMS axes orientation
            * 'det': pixels, in raw detector read out axes orientation
            * 'idl': arcsecs relative to aperture reference location.

    xnew_coords : None or ndarray
        Array of x-values in new coordinate frame to interpolate onto.
        Can be a 1-dimensional array of unique values, in which case 
        the final image will be of size (ny_new, nx_new). Or a 2d array 
        that corresponds to full regular grid and has same shape as 
        `ynew_coords` (ny_new, nx_new). If set to None, then final image
        is same size as input image, and coordinate grid spans the min
        and max values of siaf_ap.convert(xidl,yidl,'idl',to_frame). 
    ynew_coords : None or ndarray
        Array of y-values in new coordinate frame to interpolate onto.
        Can be a 1-dimensional array of unique values, in which case 
        the final image will be of size (ny_new, nx_new). Or a 2d array 
        that corresponds to full regular grid and has same shape as 
        `xnew_coords` (ny_new, nx_new). If set to None, then final image
        is same size as input image, and coordinate grid spans the min
        and max values of siaf_ap.convert(xidl,yidl,'idl',to_frame). 
    return_coords : bool
        In addition to returning the final image, setting this to True
        will return the full set of new coordinates. Output will then
        be (psf_new, xnew, ynew), where all three array have the same
        shape.
    aper : None or :mod:`pysiaf.Aperture`
        Option to pass the SIAF aperture if it is already known or
        specified to save time on generating a new one. If set to None,
        then automatically determines a new `pysiaf` aperture based on
        information stored in the header.
    sci_cen : tuple or None
        Science pixel values associated with center of array. If set to
        None, then will grab values from DET_X and DET_Y header keywords.
    pixelscale : float or None
        Pixel scale of input image in arcsec/pixel. If set to None, then
        will search for PIXELSCL and PIXSCALE keywords in header.
    oversamp : int or None
        Oversampling of input image relative to native detector pixel scale.
        If set to None, will search for OSAMP and DET_SAMP keywords. 
    """

    import pysiaf
    from scipy.interpolate import RegularGridInterpolator

    def _get_default_siaf(instrument, aper_name):
        si_match = {
            'NIRCAM' : siaf_nrc, 
            'NIRSPEC': siaf_nis, 
            'MIRI'   : siaf_mir, 
            'NIRISS' : siaf_nrs, 
            'FGS'    : siaf_fgs,
            }

        # Select a single SIAF aperture
        siaf = si_match[instrument.upper()]
        aper = siaf.apertures[aper_name]

        return aper

    # Read in input PSF
    if isinstance(hdulist_or_filename, str):
        hdu_list = fits.open(hdulist_or_filename)
    elif isinstance(hdulist_or_filename, fits.HDUList):
        hdu_list = hdulist_or_filename
    else:
        raise ValueError("input must be a filename or HDUlist")

    if aper is None:
        # Log instrument and detector names
        instrument = hdu_list[0].header["INSTRUME"].upper()
        aper_name = hdu_list[0].header["APERNAME"].upper()
        # Pull default values
        aper = _get_default_siaf(instrument, aper_name)
    
    # Pixel scale information
    ny, nx = hdu_list[ext].shape
    if pixelscale is None:
        # Pixel scale of input image
        try: pixelscale = hdu_list[ext].header["PIXELSCL"]
        except: pixelscale = hdu_list[ext].header["PIXSCALE"]
    if oversamp is None:
        # Image oversampling relative to detector
        try: oversamp = hdu_list[ext].header["OSAMP"]   
        except: oversamp = hdu_list[ext].header["DET_SAMP"]

    # Get 'sci' reference location where PSF is observed
    if sci_cen is None:
        xsci_cen = hdu_list[ext].header["DET_X"]  # center x location in pixels ('sci')
        ysci_cen = hdu_list[ext].header["DET_Y"]  # center y location in pixels ('sci')
    else:
        xsci_cen, ysci_cen = sci_cen

    # ###############################################
    # Create an array of indices (in pixels) for where the PSF is located on the detector
    nx_half, ny_half = ( (nx-1)/2., (ny-1)/2. )
    xlin = np.linspace(-1*nx_half, nx_half, nx)
    ylin = np.linspace(-1*ny_half, ny_half, ny)
    xarr, yarr = np.meshgrid(xlin, ylin) 

    # Convert the PSF center point from pixels to arcseconds using pysiaf
    xidl_cen, yidl_cen = aper.sci_to_idl(xsci_cen, ysci_cen)

    # Get 'idl' coords
    xidl = xarr * pixelscale + xidl_cen
    yidl = yarr * pixelscale + yidl_cen

    # ###############################################
    # Create an array of indices (in pixels) that the final data will be interpolated onto
    xnew_cen, ynew_cen = aper.convert(xsci_cen, ysci_cen, 'sci', to_frame)
    # If new x and y values are specified, create a meshgrid
    if (xnew_coords is not None) and (ynew_coords is not None):
        if len(xnew_coords.shape)==1 and len(ynew_coords.shape)==1:
            xnew, ynew = np.meshgrid(xnew_coords, ynew_coords)
        elif len(xnew_coords.shape)==2 and len(ynew_coords.shape)==2:
            assert xnew_coords.shape==ynew_coords.shape, "If new x and y inputs are a grid, must be same shapes"
            xnew, ynew = xnew_coords, ynew_coords
    elif to_frame=='sci':
        xnew = xarr / oversamp + xnew_cen
        ynew = yarr / oversamp + ynew_cen
    else:
        xv, yv = aper.convert(xidl, yidl, 'idl', to_frame)
        xmin, xmax = (xv.min(), xv.max())
        ymin, ymax = (yv.min(), yv.max())
        
        # Range xnew from 0 to 1
        xnew = xarr - xarr.min()
        xnew /= xnew.max()
        # Set to xmin to xmax
        xnew = xnew * (xmax - xmin) + xmin
        # Make sure center value is xnew_cen
        xnew += xnew_cen - np.median(xnew)

        # Range ynew from 0 to 1
        ynew = yarr - yarr.min()
        ynew /= ynew.max()
        # Set to ymin to ymax
        ynew = ynew * (ymax - ymin) + ymin
        # Make sure center value is xnew_cen
        ynew += ynew_cen - np.median(ynew)
    
    # Convert requested coordinates to 'idl' coordinates
    xnew_idl, ynew_idl = aper.convert(xnew, ynew, to_frame, 'idl')

    # ###############################################
    # Interpolate using Regular Grid Interpolator
    xvals = xlin * pixelscale + xidl_cen
    yvals = ylin * pixelscale + yidl_cen
    func = RegularGridInterpolator((yvals,xvals), hdu_list[ext].data, method='linear', 
                                   bounds_error=False, fill_value=fill_value)

    # Create an array of (yidl, xidl) values to interpolate onto
    pts = np.array([ynew_idl.flatten(),xnew_idl.flatten()]).transpose()
    psf_new = func(pts).reshape(xnew.shape)

    # Make sure we're not adding flux to the system via interpolation artifacts
    sum_orig = hdu_list[ext].data.sum()
    sum_new = psf_new.sum()
    if sum_new > sum_orig:
        psf_new *= (sum_orig / sum_new)
    
    if return_coords:
        return (psf_new, xnew, ynew)
    else:
        return psf_new


def image_convolution(image, psf, method='scipy', use_fft=None, **kwargs):

    """ Perform image convolution with a PSF kernel
    
    Can use either scipy or astropy convolution methods. 
    Both should produce the same results.
    """

    if len(image.shape)==3:
        return np.array([image_convolution(im, psf) for im in image])
    elif len(image.shape)==2:
        pass
    elif len(image.shape)>3:
        raise ValueError(f"Input image must have 2 or 3 dimensions. ndim={len(image.shape)}")

    from scipy.signal import choose_conv_method
    if use_fft is None:
        res = choose_conv_method(image, psf, mode='same')
        use_fft = (res == 'fft')

    use_scipy = False
    use_astropy = False
    if 'scipy' in method.lower():
        use_scipy = True
    elif 'astropy' in method.lower():
        use_astropy = True
    else:
        raise ValueError(f"Method '{method}' not recognized. Must be 'scipy' or 'astropy'.")

    if use_astropy:
        import astropy.convolution

        if use_fft:
            from scipy import fftpack
            cfunc = astropy.convolution.convolve_fft
            kwargs['fftn'] = fftpack.fftn
            kwargs['ifftn'] = fftpack.ifftn
            kwargs['allow_huge'] = True
        else:
            # Check if PSF shape is odd in both dimensions
            if (psf.shape[0] % 2 == 0):
                _log.warning("PSF shape is even along y-axis. Trimming last row.")
                psf = psf[:-1,:]
            if (psf.shape[1] % 2 == 0):
                _log.warning("PSF shape is even in x-axis. Trimming last column.")
                psf = psf[:,:-1]
            cfunc = astropy.convolution.convolve

        # Normalize PSF sum to 1.0
        norm = psf.sum()
        return norm * cfunc(image, psf/norm, normalize_kernel=False, **kwargs)
        
    elif use_scipy:
        import scipy.signal
        if use_fft is None:
            kwargs['method'] = 'auto'
        else:
            kwargs['method'] = 'fft' if use_fft else 'direct'

        kwargs['mode'] = kwargs.get('mode', 'same')
        return scipy.signal.convolve(image, psf, **kwargs)


def _convolve_psfs_for_mp(arg_vals):
    """
    Internal helper routine for parallelizing computations across multiple processors,
    specifically for convolving position-dependent PSFs with an extended image or
    field of PSFs.

    """
    
    im, psf, ind_mask = arg_vals

    ny, nx = im.shape
    ny_psf, nx_psf = psf.shape

    try:
        # Get region to perform convolution
        xtra_pix = int(nx_psf/2 + 10)
        ind = np.argwhere(ind_mask.sum(axis=0)>0)
        ix1, ix2 = (np.min(ind), np.max(ind)+1)
        ix1 -= xtra_pix
        ix1 = 0 if ix1<0 else ix1
        ix2 += xtra_pix
        ix2 = nx if ix2>nx else ix2
        
        xtra_pix = int(ny_psf/2 + 10)
        ind = np.argwhere(ind_mask.sum(axis=1))
        iy1, iy2 = (np.min(ind), np.max(ind)+1)
        iy1 -= xtra_pix
        iy1 = 0 if iy1<0 else iy1
        iy2 += xtra_pix
        iy2 = ny if iy2>ny else iy2
    except ValueError:
        # No valid data in the image
        return 0
    
    im_temp = im.copy()
    im_temp[~ind_mask] = 0
    
    # No need to convolve anything if no flux!
    if not np.allclose(im_temp,0):
        im_temp[iy1:iy2,ix1:ix2] = image_convolution(im_temp[iy1:iy2,ix1:ix2], psf)

    return im_temp


# def _convolve_psfs_for_mp_old(arg_vals):
#     """
#     Internal helper routine for parallelizing computations across multiple processors,
#     specifically for convolving position-dependent PSFs with an extended image or
#     field of PSFs.

#     """
    
#     im, psf, ind_mask = arg_vals
#     im_temp = im.copy()
#     im_temp[~ind_mask] = 0
    
#     if np.allclose(im_temp,0):
#         # No need to convolve anything if no flux!
#         res = im_temp
#     else:
#         # Normalize PSF sum to 1.0
#         # Otherwise convolve_fft may throw an error if psf.sum() is too small
#         norm = psf.sum()
#         psf = psf / norm
#         res = convolve_fft(im_temp, psf, fftn=fftpack.fftn, ifftn=fftpack.ifftn, allow_huge=True)
#         res *= norm

#     return res

def _crop_hdul(hdul_sci_image, psf_shape):

    # Science image aperture info
    im_input = hdul_sci_image[0].data
    hdr_im = hdul_sci_image[0].header

    # Crop original image in case of unnecessary zeros
    zmask = im_input!=0
    row_sum = zmask.sum(axis=0)
    col_sum = zmask.sum(axis=1)
    indx = np.where(row_sum>0)[0]
    indy = np.where(col_sum>0)[0]
    try:
        ix1, ix2 = indx[0], indx[-1]+1
    except IndexError:
        # In case all zeroes
        ix1 = int(im_input.shape[1] / 2)
        ix2 = ix1 + 1
    try:
        iy1, iy2 = indy[0], indy[-1]+1
    except IndexError:
        # In case all zeroes
        iy1 = int(im_input.shape[0] / 2)
        iy2 = iy1 + 1

    # Expand indices to accommodate PSF size
    ny_psf, nx_psf = psf_shape
    ny_im, nx_im = im_input.shape
    ix1 -= int(nx_psf/2 + 5)
    ix2 += int(nx_psf/2 + 5)
    iy1 -= int(ny_psf/2 + 5)
    iy2 += int(ny_psf/2 + 5)

    # Make sure we don't go out of bounds
    if ix1<0:     ix1 = 0
    if ix2>nx_im: ix2 = nx_im
    if iy1<0:     iy1 = 0
    if iy2>ny_im: iy2 = ny_im

    # Make HDU and copy header info
    hdu = fits.PrimaryHDU(im_input[iy1:iy2,ix1:ix2])
    try:
        hdu.header['XIND_REF'] = hdr_im['XIND_REF'] - ix1
        hdu.header['YIND_REF'] = hdr_im['YIND_REF'] - iy1
    except:
        try:
            hdu.header['XCEN'] = hdr_im['XCEN'] - ix1
            hdu.header['YCEN'] = hdr_im['YCEN'] - iy1
        except:
            hdu.header['XIND_REF'] = im_input.shape[1] / 2 - ix1
            hdu.header['YIND_REF'] = im_input.shape[0] / 2 - iy1

    hdu.header['CFRAME'] = hdr_im['CFRAME']
    if 'PIXELSCL' in hdr_im.keys():
        hdu.header['PIXELSCL'] = hdr_im['PIXELSCL']
    if 'OSAMP' in hdr_im.keys():
        hdu.header['OSAMP'] = hdr_im['OSAMP']

    hdu.header['APERNAME'] = hdr_im['APERNAME']

    hdu.header['IX1'] = ix1
    hdu.header['IX2'] = ix2
    hdu.header['IY1'] = iy1
    hdu.header['IY2'] = iy2

    return fits.HDUList([hdu])



def convolve_image(hdul_sci_image, hdul_psfs, return_hdul=False, 
                   output_sampling=None, crop_zeros=True):
    """ Convolve image with various PSFs

    Takes an extended image, breaks it up into subsections, then
    convolves each subsection with the nearest neighbor PSF. The
    subsection sizes and locations are determined from PSF 'sci'
    positions.

    Parameters
    ==========
    hdul_sci_image : HDUList
        Image to convolve. Requires header info of:
            - APERNAME : SIAF aperture that images is placed in
            - PIXELSCL : Pixel scale of image (arcsec/pixel)
            - OSAMP    : Oversampling relative to detector pixels
            - CFRAME   : Coordinate frame of image ('sci', 'tel', 'idl', 'det')
            - XCEN     : Image x-position corresponding to aperture reference location
            - YCEN     : Image y-position corresponding to aperture reference location
            - XIND_REF, YIND_REF : Alternative for (XCEN, YCEN)
    hdul_psfs : HDUList
        Multi-extension FITS. Each HDU element is a different PSF for
        some location within some field of view. Must have same pixel
        scale as hdul_sci_image.

    Keyword Args
    ============
    return_hdul : bool
        Return as an HDUList, otherwise return as an image.
    output_sampling : None or int
        Sampling output relative to detector.
        If None, then return same sampling as input image.
    crop_zeros : bool
        For large images that are zero-padded, this option will first crop off the
        extraneous zeros (but accounting for PSF size to not tuncate resulting
        convolution at edges), then place the convolved subarray image back into
        a full frame of zeros. This process can improve speeds by a factor of a few,
        with no resulting differences. Should always be set to True; only provided 
        as an option for debugging purposes.
    """
    
    import pysiaf

    # Get SIAF aperture info
    hdr_psf = hdul_psfs[0].header

    si_match = {
        'NIRCAM' : siaf_nrc, 
        'NIRSPEC': siaf_nis, 
        'MIRI'   : siaf_mir, 
        'NIRISS' : siaf_nrs, 
        'FGS'    : siaf_fgs,
        }

    # Select a single SIAF aperture
    siaf = si_match[hdr_psf['INSTRUME'].upper()]
    siaf_ap_psfs = siaf[hdr_psf['APERNAME']]

    if crop_zeros:
        hdul_sci_image_orig = hdul_sci_image
        hdul_sci_image = _crop_hdul(hdul_sci_image, hdul_psfs[0].data.shape)

    # Science image aperture info
    im_input = hdul_sci_image[0].data
    hdr_im = hdul_sci_image[0].header
    siaf_ap_sci = siaf[hdr_im['APERNAME']]
    
    # Get tel coordinates for all PSFs
    xvals = np.array([hdu.header['XVAL'] for hdu in hdul_psfs])
    yvals = np.array([hdu.header['YVAL'] for hdu in hdul_psfs])
    if 'tel' in hdr_psf['CFRAME']:
        xtel_psfs, ytel_psfs = (xvals, yvals)
    else:
        xtel_psfs, ytel_psfs = siaf_ap_psfs.convert(xvals, yvals, hdr_psf['CFRAME'], 'tel')
    
    # Get tel coordinates for every pixel in science image
    # Size of input image in arcsec
    ysize, xsize = im_input.shape
    # Image index corresponding to reference point
    try:
        xcen_im = hdr_im['XIND_REF']
        ycen_im = hdr_im['YIND_REF']
    except:
        try:
            xcen_im = hdr_im['XCEN']
            ycen_im = hdr_im['YCEN']
        except:
            ycen_im, xcen_im = get_im_cen(im_input)

    try:
        pixscale = hdr_im['PIXELSCL']
    except:
        pixscale = hdul_psfs[0].header['PIXELSCL']

    xvals_im = np.arange(xsize).astype('float') - xcen_im
    yvals_im = np.arange(ysize).astype('float') - ycen_im
    xarr_im, yarr_im = np.meshgrid(xvals_im, yvals_im)
    xref, yref = siaf_ap_sci.reference_point(hdr_im['CFRAME'])
    if (hdr_im['CFRAME'] == 'tel') or (hdr_im['CFRAME'] == 'idl'):
        xarr_im *= pixscale 
        xarr_im += xref
        yarr_im *= pixscale
        yarr_im += yref
    elif (hdr_im['CFRAME'] == 'sci') or (hdr_im['CFRAME'] == 'det'):
        xarr_im /= hdr_im['OSAMP']
        xarr_im += xref
        yarr_im /= hdr_im['OSAMP']
        yarr_im += yref

    # Convert each element in image array to tel coords
    xtel_im, ytel_im = siaf_ap_sci.convert(xarr_im, yarr_im, hdr_im['CFRAME'], 'tel')

    # Create mask for input image for each PSF to convolve
    # For each pixel, find PSF that is closest on the sky
    # Go row-by-row to save on memory
    npsf = len(hdul_psfs)
    mask_arr = np.zeros([npsf, ysize, xsize], dtype='bool')
    for iy in range(ysize):
        rho_arr = (xtel_im[iy].reshape([-1,1]) - xtel_psfs.reshape([1,-1]))**2 \
                + (ytel_im[iy].reshape([-1,1]) - ytel_psfs.reshape([1,-1]))**2

        # Calculate indices corresponding to closest PSF for each pixel
        im_ind = np.argmin(rho_arr, axis=1)

        mask = np.asarray([im_ind==i for i in range(npsf)])
        mask_arr[:,iy,:] = mask
        
    del rho_arr, im_ind, mask, xtel_im, ytel_im
    
    # Make sure all pixels have a mask value of 1 somewhere (and only in one mask!)
    mask_sum = mask_arr.sum(axis=0)
    ind_bad = (mask_sum != 1)
    nbad = len(mask_sum[ind_bad])
    assert np.allclose(mask_sum, 1), f"{nbad} pixels in mask not assigned a PSF."

    # Split into workers
    im_conv = np.zeros_like(im_input)
    worker_args = [(im_input, hdul_psfs[i].data, mask_arr[i]) for i in range(npsf)]
    # itervals = tqdm(worker_args, desc='Convolution', leave=False)
    itervals = worker_args
    for wa in itervals:
        im_conv += _convolve_psfs_for_mp(wa)

    # Ensure there are no negative values from convolve_fft
    im_conv[im_conv<0] = 0

    # If we cropped the original input, put convolved image into full array
    if crop_zeros:
        hdul_sci_image_crop = hdul_sci_image
        hdul_sci_image = hdul_sci_image_orig

        im_conv_crop = im_conv
        im_conv = np.zeros_like(hdul_sci_image[0].data)

        hdr_crop = hdul_sci_image_crop[0].header

        ix1, ix2 = (hdr_crop['IX1'], hdr_crop['IX2'])
        iy1, iy2 = (hdr_crop['IY1'], hdr_crop['IY2'])

        im_conv[iy1:iy2,ix1:ix2] = im_conv_crop

    # Scale to specified output sampling
    if output_sampling is None:
        scale = 1
        output_sampling = hdr_im['OSAMP']
    else:
        scale = output_sampling / hdr_im['OSAMP']
    im_conv = frebin(im_conv, scale=scale)

    if return_hdul:
        hdul = deepcopy(hdul_sci_image)
        hdul[0].data = im_conv
        hdul[0].header['OSAMP'] = output_sampling
        return hdul
    else:
        return im_conv


def make_disk_image(inst, disk_params, sp_star=None, pixscale_out=None, dist_out=None,
                    shape_out=None):
    """
    Rescale disk model flux to desired pixel scale and distance.
    If instrument bandpass is different from disk model, scales 
    flux assuming a grey scattering model.

    Returns image flux values in photons/sec.
    
    Parameters
    ==========
    inst : mod::webbpsf_ext instrument class
        E.g. NIRCam_ext, MIRI_ext classes
    disk_params : dict
        Arguments describing the necessary model information:
            - 'file'       : Path to model file or an HDUList.
            - 'pixscale'   : Pixel scale (arcsec/pixel).
            - 'dist'       : Assumed model distance in parsecs.
            - 'wavelength' : Wavelength of observation in microns.
            - 'units'      : String of assumed flux units (ie., MJy/arcsec^2 or muJy/pixel)
            - 'cen_star'   : True/False. Is a star already placed in the central pixel? 
        Will Convert from [M,m,u,n]Jy/[arcsec^2,pixel] to photons/sec/pixel

    Keyword Args
    ============
    sp_star : :class:`webbpsf_ext.synphot_ext.Spectrum`
        A synphot spectrum of central star. Used to adjust observed
        photon flux if filter differs from model input
    pixscale_out : float
        Desired pixelscale of returned image. If None, then use instrument's
        oversampled pixel scale.
    dist_out : float
        Distance to place disk at. Flux is scaled appropriately relative to
        the input distance specified in `disk_params`.
    shape_out : tuple, int, or None
        Desired size for the output array (ny,nx). If a single value, then will 
        create a 2-element tuple of the same value.
    """

    from .spectra import stellar_spectrum
    
    # Get stellar spectrum
    if sp_star is None:
        sp_star = stellar_spectrum('flat')
        
    # Set desired distance to be the same as the stellar object
    if dist_out is None:
        dist_out = disk_params['dist']
    
    # Create disk image for input bandpass from model
    keys = ['file', 'pixscale', 'dist', 'wavelength', 'units']
    args_model = tuple(disk_params[k] for k in keys)

    # Open model file and scale disk emission to new bandpass, assuming grey scattering properties
    hdul_model = model_to_hdulist(args_model, sp_star, inst.bandpass)

    # Change pixel scale (default is same as inst pixel oversampling)
    # Provide option to move disk to a different distance
    # `dist_in` and `pixscale_in` will be pulled from HDUList header
    if pixscale_out is None:
        pixscale_out = inst.pixelscale / inst.oversample
    hdul_disk_image = image_rescale(hdul_model, pixscale_out, dist_out=dist_out, 
                                    cen_star=disk_params['cen_star'], shape_out=shape_out)

    # copy_keys = [
    #     'INSTRUME', 'APERNAME', 'FILTER', 'DET_SAMP',
    #     'DET_NAME', 'DET_X', 'DET_Y', 'DET_V2', 'DET_V3',
    # ]
    # head_temp = inst.psf_coeff_header
    # for key in copy_keys:
    #     try:
    #         hdul_disk_image[0].header[key] = (head_temp[key], head_temp.comments[key])
    #     except (AttributeError, KeyError):
    #         pass

    # Make sure these keywords match current instrument aperture,
    # which could be different from PSF-generated aperture name.
    hdul_disk_image[0].header['INSTRUME'] = inst.name
    hdul_disk_image[0].header['FILTER'] = inst.filter
    hdul_disk_image[0].header['OSAMP'] = inst.oversample
    hdul_disk_image[0].header['DET_SAMP'] = inst.oversample
    hdul_disk_image[0].header['DET_NAME'] = inst.aperturename.split('_')[0]
    siaf_ap = inst.siaf_ap
    hdul_disk_image[0].header['APERNAME'] = siaf_ap.AperName
    hdul_disk_image[0].header['DET_X']    = siaf_ap.XSciRef
    hdul_disk_image[0].header['DET_Y']    = siaf_ap.YSciRef
    hdul_disk_image[0].header['DET_V2']   = siaf_ap.V2Ref
    hdul_disk_image[0].header['DET_V3']   = siaf_ap.V3Ref
        
    return hdul_disk_image

def rotate_shift_image(hdul, index=0, angle=0, delx_asec=0, dely_asec=0, 
                       shift_func=fshift, reshape=False, **kwargs):
    """ Rotate/Shift image
    
    Rotate then offset image by some amount.
    Positive angles rotate the image counter-clockwise.
    
    Parameters
    ==========
    hdul : HDUList
        Input HDUList
    index : int
        Specify HDU index, usually 0
    angle : float
        Rotate entire scene by some angle. 
        Positive angles rotate counter-clockwise.
    delx_asec : float
        Offset in x direction (specified in arcsec). 
        Pixel scale should be included in header keyword 'PIXELSCL'.
    dely_asec : float
        Offset in x direction (specified in arcsec). 
        Pixel scale should be included in header keyword 'PIXELSCL'.
    shift_func : function
        Function to use for shifting. Usually either `fshift` or `fourier_imshift`.

    Keyword Args
    ============
    order : int, optional
        The order of the spline interpolation, default is 3.
        The order has to be in the range 0-5. This also determines the 
        interpolation value of the shift function if `interp` is not set.
        if order <=1: interp='linear'; if order <=3; otherwise interp='cubic'.
    interp : str, optional
        Interpolation method to use for shifting using 'fshift' or 'opencv. 
        If not set, will default to method as described by `order` keyword.
        For 'opencv', valid options are 'linear', 'cubic', and 'lanczos'.
        for 'fshift', valid options are 'linear', 'cubic', and 'quintic'.
    else:
        interp='quintic'
    mode : {'reflect', 'constant', 'nearest', 'mirror', 'wrap'}, optional
        The `mode` parameter determines how the input array is extended
        beyond its boundaries. Default is 'constant'. Behavior for each valid
        value is as follows:

        'reflect' (`d c b a | a b c d | d c b a`)
            The input is extended by reflecting about the edge of the last
            pixel.

        'constant' (`k k k k | a b c d | k k k k`)
            The input is extended by filling all values beyond the edge with
            the same constant value, defined by the `cval` parameter.

        'nearest' (`a a a a | a b c d | d d d d`)
            The input is extended by replicating the last pixel.

        'mirror' (`d c b | a b c d | c b a`)
            The input is extended by reflecting about the center of the last
            pixel.

        'wrap' (`a b c d | a b c d | a b c d`)
            The input is extended by wrapping around to the opposite edge.
    cval : scalar, optional
        Value to fill past edges of input if `mode` is 'constant'. Default
        is 0.0.
    prefilter : bool, optional
        Determines if the input array is prefiltered with `spline_filter`
        before interpolation. The default is True, which will create a
        temporary `float64` array of filtered values if `order > 1`. If
        setting this to False, the output will be slightly blurred if
        `order > 1`, unless the input is prefiltered, i.e. it is the result
        of calling `spline_filter` on the original input.
    """
    
    # from copy import deepcopy

    PA_offset = kwargs.get('PA_offset')
    if PA_offset is not None:
        _log.warning('`PA_offset` is deprecated. Please use `angle` keyword instead. Setting angle=PA_offset for now.')
        angle = PA_offset

    interp = kwargs.pop('interp', None)
    # Get position offsets
    if interp is None:
        order = kwargs.get('order', 3)
        if order <=1:
            interp='linear'
        elif order <=3:
            interp='cubic'
        else:
            interp='quintic'

    # Rotate
    im_rot = rotate_offset(hdul[index].data, -1*angle, reshape=reshape, **kwargs)
    
    # Shift
    delx, dely = np.array([delx_asec, dely_asec]) / hdul[0].header['PIXELSCL']
    if reshape:
        # Pad based on shift values
        # pad_x1 = int(np.abs(np.floor(delx))) if delx < 0 else 0
        # pad_x2 = int(np.abs(np.ceil(delx))) if delx > 0 else 0
        # pad_y1 = int(np.abs(np.floor(dely))) if dely < 0 else 0
        # pad_y2 = int(np.abs(np.ceil(dely))) if dely > 0 else 0
        # pad = ((pad_y1, pad_y2), (pad_x1, pad_x2))
        padx = int(np.ceil(np.abs(delx)))
        pady = int(np.ceil(np.abs(dely)))
        pad = ((pady,pady), (padx,padx))
        im_rot = np.pad(im_rot, pad)
    im_new = shift_func(im_rot, delx, dely, pad=False, interp=interp)
    
    # Create new HDU and copy header
    hdu_new = fits.PrimaryHDU(im_new)
    hdu_new.header = hdul[index].header
    return fits.HDUList(hdu_new)

    # Copy and replace specified index
    # hdu_new = deepcopy(hdul)
    # hdu_new[index] = im_new
    # return hdu_new
    
def crop_zero_rows_cols(image, symmetric=True, return_indices=False):
    """Crop off rows and columns that are all zeros."""

    zmask = (image!=0)

    row_sum = zmask.sum(axis=0)
    col_sum = zmask.sum(axis=1)

    if symmetric:
        nx1 = np.where(row_sum>0)[0][0]
        nx2 = np.where(row_sum[::-1]>0)[0][0]

        ny1 = np.where(col_sum>0)[0][0]
        ny2 = np.where(col_sum[::-1]>0)[0][0]

        crop_border = np.min([nx1,nx2,ny1,ny2])
        ix1 = iy1 = crop_border
        ix2 = image.shape[1] - crop_border
        iy2 = image.shape[0] - crop_border
    else:
        indx = np.where(row_sum>0)[0]
        indy = np.where(col_sum>0)[0]
        ix1, ix2 = indx[0], indx[-1]+1
        iy1, iy2 = indy[0], indy[-1]+1

    im_new = image[iy1:iy2,ix1:ix2]

    if return_indices:
        return im_new, [ix1,ix2,iy1,iy2]
    else:
        return im_new

def expand_mask(bpmask, npix, grow_diagonal=False):
    """Expand bad pixel mask by npix pixels
    
    Parameters
    ==========
    bpmask : 2D, 3D+ array
        Boolean bad pixel mask
    npix : int
        Number of pixels to expand mask by
    diagonal : bool
        Expand mask by npix pixels in all directions, including diagonals
    in_place : bool
        Modify the original mask (True) or return a copy (False)

    Returns
    =======
    bpmask : 2D array of booleans
        Expanded bad pixel mask
    """
    from scipy.ndimage import binary_dilation, generate_binary_structure

    if npix==0:
        return bpmask
    
    # Check dimensions
    ndim = bpmask.ndim
    # If 3D or more, then apply recursively
    if ndim>2:
        # Reshape into cube and expand image by image
        sh_orig = bpmask.shape
        ny, nx = bpmask.shape[-2:]
        bpmask.reshape([-1,ny,nx])

        res = np.array([expand_mask(im, npix, grow_diagonal=grow_diagonal) for im in bpmask])
        return res.reshape(sh_orig)

    # Expand mask by npix pixels, including corners
    if grow_diagonal:
        # Perform normal dilation without corners (just left, right, up, down)
        if npix>1:
            bpmask = binary_dilation(bpmask, iterations=npix-1)
        # Add corners in final iteration
        struct2 = generate_binary_structure(2, 2)
        bpmask = binary_dilation(bpmask, structure=struct2)
    else: # No corners
        bpmask = binary_dilation(bpmask, iterations=npix)

    return bpmask

def bp_fix(im, sigclip=5, niter=1, pix_shift=1, rows=True, cols=True, corners=True,
           bpmask=None, return_mask=False, verbose=False, in_place=True):
    """ Find and fix bad pixels in image with median of surrounding values
    
    Paramters
    ---------
    im : ndarray
        Single image
    sigclip : int
        How many sigma from mean doe we fix?
    niter : int
        How many iterations for sigma clipping? 
        Ignored if bpmask is set.
    pix_shift : int
        We find bad pixels by comparing to neighbors and replacing.
        E.g., if set to 1, use immediate adjacents neighbors.
        Replaces with a median of surrounding pixels.
    rows : bool
        Compare to row pixels? Setting to False will ignore pixels
        along rows during comparison. Recommended to increase
        ``pix_shift`` parameter if using only rows or cols.
    cols : bool
        Compare to column pixels? Setting to False will ignore pixels
        along columns during comparison. Recommended to increase
        ``pix_shift`` parameter if using only rows or cols.
    corners : bool
        Include corners in neighbors? If False, then only use
        pixels that are directly adjacent for comparison.
    bpmask : boolean array
        Use a pre-determined bad pixel mask for fixing.
    return_mask : bool
        If True, then also return a masked array of bad
        pixels where a value of 1 is "bad".
    verbose : bool
        Print number of fixed pixels per iteration
    in_place : bool
        Do in-place corrections of input array.
        Otherwise, return a copy.
    """

    from . import robust
    
    def shift_array(arr_out, pix_shift, rows=True, cols=True, corners=True):
        '''Create an array of shifted values'''

        shift_arr = []
        sh_vals = np.arange(pix_shift*2+1) - pix_shift
        # Set shifting of columns and rows
        xsh_vals = sh_vals if rows else [0]
        ysh_vals = sh_vals if cols else [0]
        for i in xsh_vals:
            for j in ysh_vals:
                is_center = (i==0) & (j==0)
                is_corner = (np.abs(i)==pix_shift) & (np.abs(j)==pix_shift)
                skip = (is_center) or (is_corner and not corners)
                if not skip:
                    shift_arr.append(fshift(arr_out, delx=i, dely=j, 
                                            pad=True, cval=0))
        shift_arr = np.asarray(shift_arr)
        return shift_arr
        
    if in_place:
        arr_out = im
    else:
        arr_out = im.copy()
    maskout = np.zeros(im.shape, dtype='bool')
    
    for ii in range(niter):
        # Create an array of shifted values
        shift_arr = shift_array(arr_out, pix_shift, corners=corners,
                                rows=rows, cols=cols)
    
        if bpmask is None:
            # Take median of shifted values
            shift_med = np.nanmedian(shift_arr, axis=0)
            # Standard deviation of shifted values
            shift_std = robust.medabsdev(shift_arr, axis=0)

            # Difference of median and reject outliers
            diff = np.abs(arr_out - shift_med)
            indbad = diff > (sigclip*shift_std)

            # Mark anything that is a NaN
            indbad[np.isnan(arr_out)] = True
        elif ii==0:
            indbad = bpmask.copy()
        else:
            indbad = np.zeros_like(arr_out, dtype='bool')

        # Mark anything that is a NaN
        indbad[np.isnan(arr_out)] = True

        # Update median shifted values to those with good pixels only
        ibad_arr = shift_array(indbad, pix_shift, corners=corners,
                               rows=rows, cols=cols)
        shift_arr[ibad_arr] = np.nan
        shift_med = np.nanmean(shift_arr, axis=0)
        
        # Set output array and mask values 
        arr_out[indbad] = shift_med[indbad]
        maskout[indbad] = True
        
        if verbose:
            print(f'Bad Pixels fixed: {indbad.sum()}')

        # Break if no bad pixels remaining
        if (indbad.sum()==0) and (np.isnan(arr_out).sum()==0):
            break
            
    if return_mask:
        return arr_out, maskout
    else:
        return arr_out


def add_ipc(im, alpha_min=0.0065, alpha_max=None, kernel=None):
    """Convolve image with IPC kernel
    
    Given an image in electrons, apply IPC convolution.
    NIRCam average IPC values (alpha) reported 0.005 - 0.006.
    
    Parameters
    ==========
    im : ndarray
        Input image or array of images.
    alpha_min : float
        Minimum coupling coefficient between neighboring pixels.
        If alpha_max is None, then this is taken to be constant
        with respect to signal levels.
    alpha_max : float or None
        Maximum value of coupling coefficent. If specificed, then
        coupling between pixel pairs is assumed to vary depending
        on signal values. See Donlon et al., 2019, PASP 130.
    kernel : ndarry or None
        Option to directly specify the convolution kernel. 
        `alpha_min` and `alpha_max` are ignored.
    
    Examples
    ========
    Constant Kernel

        >>> im_ipc = add_ipc(im, alpha_min=0.0065)

    Constant Kernel (manual)

        >>> alpha = 0.0065
        >>> k = np.array([[0,alpha,0], [alpha,1-4*alpha,alpha], [0,alpha,0]])
        >>> im_ipc = add_ipc(im, kernel=k)

    Signal-dependent Kernel

        >>> im_ipc = add_ipc(im, alpha_min=0.0065, alpha_max=0.0145)

    """
    
    sh = im.shape
    ndim = len(sh)
    if ndim==2:
        im = im.reshape([1,sh[0],sh[1]])
        sh = im.shape
    
    if kernel is None:
        xp = yp = 1
    else:
        yp, xp = np.array(kernel.shape) / 2
        yp, xp = int(yp), int(xp)

    # Pad images to have a pixel border of zeros
    im_pad = np.pad(im, ((0,0), (yp,yp), (xp,xp)), 'symmetric')
    
    # Check for custom kernel (overrides alpha values)
    if (kernel is not None) or (alpha_max is None):
        # Reshape to stack all images along horizontal axes
        im_reshape = im_pad.reshape([-1, im_pad.shape[-1]])
    
        if kernel is None:
            kernel = np.array([[0.0, alpha_min, 0.0],
                               [alpha_min, 1.-4*alpha_min, alpha_min],
                               [0.0, alpha_min, 0.0]])
    
        # Convolve IPC kernel with images
        # print('Applying IPC kernel')
        im_ipc = image_convolution(im_reshape, kernel).reshape(im_pad.shape)
    
    # Exponential coupling strength
    # Equation 7 of Donlon et al. (2018)
    else:
        arrsqr = im_pad**2

        amin = alpha_min
        amax = alpha_max
        ascl = (amax-amin) / 2
        
        alpha_arr = []
        for ax in [1,2]:
            # Shift by -1
            diff = np.abs(im_pad - np.roll(im_pad, -1, axis=ax))
            sumsqr = arrsqr + np.roll(arrsqr, -1, axis=ax)
            
            imtemp = amin + ascl * np.exp(-diff/20000) + \
                     ascl * np.exp(-np.sqrt(sumsqr / 2) / 10000)
            alpha_arr.append(imtemp)
            # Take advantage of symmetries to shift in other direction
            alpha_arr.append(np.roll(imtemp, 1, axis=ax))
            
        alpha_arr = np.array(alpha_arr)

        # Flux remaining in parent pixel
        im_ipc = im_pad * (1 - alpha_arr.sum(axis=0))
        # Flux shifted to adjoining pixels
        for i, (shft, ax) in enumerate(zip([-1,+1,-1,+1], [1,1,2,2])):
            im_ipc += alpha_arr[i]*np.roll(im_pad, shft, axis=ax)
        del alpha_arr

    # Trim excess
    im_ipc = im_ipc[:,yp:-yp,xp:-xp]
    if ndim==2:
        im_ipc = im_ipc.squeeze()
    return im_ipc
    
    
def add_ppc(im, ppc_frac=0.002, nchans=4, kernel=None,
    same_scan_direction=False, reverse_scan_direction=False,
    in_place=False):
    """ Add Post-Pixel Coupling (PPC)
    
    This effect is due to the incomplete settling of the analog
    signal when the ADC sample-and-hold pulse occurs. The measured
    signals for a given pixel will have a value that has not fully
    transitioned to the real analog signal. Mathematically, this
    can be treated in the same way as IPC, but with a different
    convolution kernel.
    
    Parameters
    ==========
    im : ndarray
        Image or array of images
    ppc_frac : float
        Fraction of signal contaminating next pixel in readout. 
    kernel : ndarry or None
        Option to directly specify the convolution kernel, in
        which case `ppc_frac` is ignored.
    nchans : int
        Number of readout output channel amplifiers.
    same_scan_direction : bool
        Are all the output channels read in the same direction?
        By default fast-scan readout direction is ``[-->,<--,-->,<--]``
        If ``same_scan_direction``, then all ``-->``
    reverse_scan_direction : bool
        If ``reverse_scan_direction``, then ``[<--,-->,<--,-->]`` or all ``<--``
    in_place : bool
        Apply in place to input image.
    """

                       
    sh = im.shape
    ndim = len(sh)
    if ndim==2:
        im = im.reshape([1,sh[0],sh[1]])
        sh = im.shape

    nz, ny, nx = im.shape
    chsize = nx // nchans
    
    # Do each channel separately
    if kernel is None:
        kernel = np.array([[0.0, 0.0, 0.0],
                           [0.0, 1.0-ppc_frac, ppc_frac],
                           [0.0, 0.0, 0.0]])

    res = im if in_place else im.copy()
    for ch in np.arange(nchans):
        if same_scan_direction:
            k = kernel[:,::-1] if reverse_scan_direction else kernel
        elif np.mod(ch,2)==0:
            k = kernel[:,::-1] if reverse_scan_direction else kernel
        else:
            k = kernel if reverse_scan_direction else kernel[:,::-1]

        x1 = chsize*ch
        x2 = x1 + chsize
        # print('  Applying PPC as IPC kernel...')
        res[:,:,x1:x2] = add_ipc(im[:,:,x1:x2], kernel=k)
    
    if ndim==2:
        res = res.squeeze()
    return res

def apply_pixel_diffusion(im, pixel_sigma):
    """Apply charge diffusion kernel to image
    
    Approximates the effect of charge diffusion as a Gaussian.

    Parameters
    ----------
    im : ndarray
        Input image.
    pixel_sigma : float
        Sigma of Gaussian kernel in units of image pixels.
    """
    from scipy.ndimage import gaussian_filter
    if pixel_sigma > 0:
        # print(f'Applying pixel diffusion of sigma={pixel_sigma} pixels')
        return gaussian_filter(im, pixel_sigma)
    else:
        return im
