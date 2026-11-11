"""Region contrast (Cheng CVPR2011 equations 5-7); see METHOD.md."""
import numpy as np
from scipy.spatial.distance import cdist
from skimage.color import rgb2lab
from skimage.segmentation import felzenszwalb

RC_PARAMS = dict(color_bins=12, retained_mass=.95, segmentation_scale=100,
                 segmentation_sigma=.5, min_size=50, spatial_denominator=.4)


def validate_rgb(rgb):
    array=np.asarray(rgb)
    if array.ndim!=3 or array.shape[2]!=3 or min(array.shape[:2])<1 or array.dtype!=np.uint8:
        raise ValueError('Expected a nonempty uint8 RGB image')
    return array


def quantize_colors(rgb):
    """12^3 RGB bins; retain frequent bins covering >=95%, remap remaining bins."""
    image=validate_rgb(rgb)
    levels=RC_PARAMS['color_bins']
    bins=image.astype(np.uint16)*levels//256
    keys=(bins[...,0]*levels*levels+bins[...,1]*levels+bins[...,2]).ravel()
    unique,inverse,counts=np.unique(keys,return_inverse=True,return_counts=True)
    pixels=image.reshape(-1,3).astype(float)/255
    means=np.column_stack([np.bincount(inverse,weights=pixels[:,c],minlength=len(unique))/counts
                           for c in range(3)])
    lab=rgb2lab(means[None,:,:])[0]
    order=np.argsort(-counts,kind='stable')
    keep_count=int(np.searchsorted(np.cumsum(counts[order]),RC_PARAMS['retained_mass']*keys.size))+1
    retained=order[:keep_count]
    # Resolve ties by stable bin order; retained bins always map to themselves.
    mapping=np.argmin(cdist(lab,lab[retained]),axis=1)
    mapping[retained]=np.arange(keep_count)
    return mapping[inverse].reshape(image.shape[:2]),lab[retained]


def regional_contrast(histograms, colors, centers, weights):
    color_distance=cdist(colors,colors)
    distances=histograms @ color_distance @ histograms.T
    spatial=np.exp(-cdist(centers,centers)/RC_PARAMS['spatial_denominator'])
    np.fill_diagonal(spatial,0)
    return np.maximum((spatial*distances) @ weights,0)


def compute_rc(rgb):
    image=validate_rgb(rgb)
    # Explicit implementation parameters; no center or border foreground prior.
    labels=felzenszwalb(image.astype(float)/255,
                       scale=RC_PARAMS['segmentation_scale'],
                       sigma=RC_PARAMS['segmentation_sigma'],
                       min_size=RC_PARAMS['min_size'],channel_axis=-1)
    _,inverse=np.unique(labels.ravel(),return_inverse=True)
    labels=inverse.reshape(labels.shape)
    sizes=np.bincount(inverse)
    color_ids,colors=quantize_colors(image)
    hist=np.zeros((len(sizes),len(colors)),float)
    np.add.at(hist,(inverse,color_ids.ravel()),1)
    hist/=sizes[:,None]
    yy,xx=np.indices(labels.shape)
    height,width=labels.shape
    centers=np.column_stack([np.bincount(inverse,weights=(xx.ravel()+.5)/width)/sizes,
                             np.bincount(inverse,weights=(yy.ravel()+.5)/height)/sizes])
    scores=regional_contrast(hist,colors,centers,sizes/sizes.sum())
    return scores[labels].astype(np.float32)
