""" Module with a wrapper around a EMOCA reconstruction model [1]_ that can be
used in Medusa. 

.. [1] Danecek, R., Black, M. J., & Bolkart, T. (2022). EMOCA: Emotion Driven Monocular
       Face Capture and Animation. *arXiv preprint arXiv:2204.11312*.
""" 

import torch
import numpy as np
from pathlib import Path

from ..utils import get_logger
from ..core import FlameReconModel
from .encoders import ResnetEncoder
from ..decoders import FLAME, DetailGenerator
from ..utils import vertex_normals, load_obj, upsample_mesh
from ..transforms import create_viewport_matrix, create_ortho_matrix, crop_matrix_to_3d

logger = get_logger()


class DecaReconModel(FlameReconModel):
    """ A 3D face reconstruction model that uses the FLAME topology.
    
    At the moment, four different models are supported: 'deca-coarse', 'deca-dense',
    'emoca-coarse', and 'emoca-dense'.

    Parameters
    ----------
    name : str  
        Either 'deca-coarse', 'deca-dense', 'emoca-coarse', or 'emoca-dense'
    img_size : tuple
        Original (before cropping!) image dimensions of video frame (width, height);
        needed for baking in translation due to cropping; if not set, it is assumed
        that the image is not cropped!
    device : str
        Either 'cuda' (uses GPU) or 'cpu'

    Attributes
    ----------
    tform : np.ndarray
        A 3x3 numpy array with the cropping transformation matrix;
        needs to be set before running the actual reconstruction!
    """    

    # May have some speed benefits
    torch.backends.cudnn.benchmark = True

    def __init__(self, name, img_size=None, device="cuda", tform=None):
        """ Initializes an DECA-like model object. """
        super().__init__()
        self.name = name
        self.img_size = img_size
        self.device = device
        self.dense = 'dense' in name
        self.tform = tform
        self._warned_about_tform = False
        self._check()
        self._load_cfg()  # sets self.cfg
        self._load_data()
        self._crop_img_size = (224, 224)
        self._create_submodels()

    def _check(self):
        """ Does some checks of the parameters. """ 
        MODELS = ['deca-coarse', 'deca-dense', 'emoca-coarse', 'emoca-dense']        
        if self.name not in MODELS:
            raise ValueError(f"Name must be in {MODELS}, but got {self.name}!")
        
        DEVICES = ['cuda', 'cpu']
        if self.device not in DEVICES:
            raise ValueError(f"Device must be in {DEVICES}, but got {self.device}!")
        
        if self.img_size is None:
            logger.warning("Arg `img_size` not given; beware, cannot render recon "
                           "on top of original image anymore (only on cropped image)")

    def _load_data(self):
        """Loads necessary data. """
        data_dir = Path(__file__).parents[1] / 'data'

        if self.dense:
            self.dense_template = np.load(data_dir / 'texture_data_256.npy',
                                          allow_pickle=True, encoding='latin1').item()           
            self.fixed_uv_dis = np.load(data_dir / 'fixed_displacement_256.npy')
            self.fixed_uv_dis = torch.tensor(self.fixed_uv_dis).float().to(self.device)

        _, uvcoords, faces, uvfaces = load_obj(data_dir / 'head_template.obj')
        self.faces = faces.to(self.device)
        self.uvcoords = uvcoords
        self.uvfaces = uvfaces

    def _create_submodels(self):
        """ Creates all EMOCA encoding and decoding submodels. To summarize:
        - `E_flame`: predicts (coarse) FLAME parameters given an image
        - `E_expression`: predicts expression FLAME parameters given an image
        - `E_detail`: predicts detail FLAME parameters given an image
        - `D_flame`: outputs a ("coarse") mesh given (shape, exp, pose) FLAME parameters
        - `D_flame_tex`: outputs a texture map given (tex) FLAME parameters
        - `D_detail`: outputs detail map (in uv space) given (detail) FLAME parameters
        """

        # set up parameter list and dict
        self.param_dict = {
            "n_shape": 100,
            "n_tex": 50,
            "n_exp": 50,
            "n_pose": 6,
            "n_cam": 3,
            "n_light": 27,
        }

        self.n_param = sum([n for n in self.param_dict.values()])

        # encoders
        self.E_flame = ResnetEncoder(outsize=self.n_param).to(self.device)

        if self.dense:
            self.E_detail = ResnetEncoder(outsize=128).to(self.device)

        if 'emoca' in self.name:
            self.E_expression = ResnetEncoder(self.param_dict["n_exp"]).to(self.device)

        # decoders
        self.D_flame = FLAME(self.cfg['flame_path'], n_shape=100, n_exp=50).to(self.device)

        if self.dense:
            latent_dim = 128 + 50 + 3  # (n_detail, n_exp, n_cam)
            self.D_detail = DetailGenerator(
                latent_dim=latent_dim,
                out_channels=1,
                out_scale=0.01,
                sample_mode="bilinear",
            ).to(self.device)

        # Load weights from checkpoint and apply to models
        checkpoint = torch.load(self.cfg[self.name.split('-')[0] + '_path'])

        self.E_flame.load_state_dict(checkpoint["E_flame"])

        if self.dense:
            self.E_detail.load_state_dict(checkpoint["E_detail"])
            self.D_detail.load_state_dict(checkpoint["D_detail"])
            self.E_detail.eval()
            self.D_detail.eval()
        
        if 'emoca' in self.name:
            self.E_expression.load_state_dict(checkpoint["E_expression"])    
            # for some reason E_exp should be explicitly cast to cuda
            self.E_expression.to(self.device)
            self.E_expression.eval()

        # Set everything to 'eval' (inference) mode
        self.E_flame.eval()
        torch.set_grad_enabled(False)  # apparently speeds up forward pass, too

    def _encode(self, image):
        """ "Encodes" the image into FLAME parameters, i.e., predict FLAME
        parameters for the given image. Note that, at the moment, it only
        works for a single image, not a batch of images.

        Parameters
        ----------
        image : torch.Tensor
            A Tensor with shape 1 (batch size) x 3 (color ch.) x 244 (w) x 244 (h)

        Returns
        -------
        enc_dict : dict
            A dictionary with all encoded parameters and some extra data needed
            for the decoding stage.
        """

        if self.img_size is None:
            # If img_size was not set upon initialization, assume no cropping
            # and use the size of the current image
            self.img_size = tuple(image.shape[2:])

        # Encode image into FLAME parameters, then decompose parameters
        # into a dict with parameter names (shape, tex, exp, etc) as keys
        # and the estimated parameters as values
        enc_params = self.E_flame(image)
        enc_dict = self._decompose_params(enc_params, self.param_dict)

        # Note to self:
        # enc_dict['cam'] contains [batch_size, x_trans, y_trans, zoom] (in mm?)
        # enc_dict['pose'] contains [rot_x, rot_y, rot_z] (in radians) for the neck
        # and jaw (first 3 are neck, last three are jaw)
        # rot_x_jaw = mouth opening
        # rot_y_jaw = lower jaw to left or right
        # rot_z_jaw = not really possible?

        # Encode image into detail parameters
        if self.dense:
            detail_params = self.E_detail(image)
            enc_dict['detail'] = detail_params

        # Replace "DECA" expression parameters with EMOCA-specific
        # expression parameters
        if 'emoca' in self.name:
            enc_dict["exp"] = self.E_expression(image)

        return enc_dict

    def _decompose_params(self, parameters, num_dict):
        """Convert a flattened parameter vector to a dictionary of parameters
        code_dict.keys() = ['shape', 'tex', 'exp', 'pose', 'cam', 'light']."""
        enc_dict = {}
        start = 0
        for key in num_dict:
            key = key[2:]  # trim off n_
            end = start + int(num_dict["n_" + key])
            enc_dict[key] = parameters[:, start:end]
            start = end
            if key == "light":
                # Reshape 27 flattened params into 9 x 3 array
                enc_dict[key] = enc_dict[key].reshape(enc_dict[key].shape[0], 9, 3)

        return enc_dict

    def _decode(self, enc_dict):
        """Decodes the face attributes (vertices, landmarks, texture, detail map)
        from the encoded parameters.

        Parameters
        ----------
        orig_size : tuple
            Tuple containing the original image size (height, width), i.e.,
            before cropping; needed to transform and render the mesh in the
            original image space

        Returns
        -------
        dec_dict : dict
            A dictionary with the results from the decoding stage

        Raises
        ------
        ValueError
            If `tform` parameter is not `None` and `orig_size` is `None`. In other
            words, if `tform` is supplied, `orig_size` should be supplied as well

        """

        # "Decode" vertices (`v`) from the predicted shape/exp/pose parameter
        v, R = self.D_flame(
            shape_params=enc_dict["shape"],
            expression_params=enc_dict["exp"],
            pose_params=enc_dict["pose"],
        )
        
        if self.dense:
            input_detail = torch.cat([enc_dict['pose'][:, 3:], enc_dict['exp'], enc_dict['detail']], dim=1)
            uv_z = self.D_detail(input_detail)
            
            normals = vertex_normals(v, self.faces.expand(1, -1, -1))
            #uv_detail_normals = self._disp2normal(uv_z, v, normals)
            disp_map = uv_z + self.fixed_uv_dis[None, None, :, :]
            v = upsample_mesh(v.cpu().numpy().squeeze(),
                              normals.cpu().numpy().squeeze(),
                              disp_map.cpu().numpy().squeeze(),
                              self.dense_template)
        else:
            v = v.cpu().numpy().squeeze()

        # Note that `v` is in world space, but pose (global rotation only)
        # is already applied
        cam = enc_dict["cam"].cpu().numpy().squeeze()  # 'camera' params

        # Now, let's define all the transformations of `v`
        # First, rotation has already been applied, which is stored in `R`
        R = R.cpu().numpy().squeeze()  # global rotation matrix

        # Actually, R is per vertex (not sure why) but doesn't really differ
        # across vertices, so let's average
        R = R.mean(axis=0)

        # Now, translation. We are going to do something weird. EMOCA (and
        # DECA) estimate translation (and scale) parameters *of the camera*,
        # not of the face. In other words, they assume the camera is is translated
        # w.r.t. the model, not the other way around (but it is technically equivalent).
        # Because we have a fixed camera and a (possibly) moving face, we actually
        # apply translation (and scale) to the model, not the camera.
        tx, ty = cam[1:]
        T = np.array([[1, 0, 0, tx], [0, 1, 0, ty], [0, 0, 1, 0], [0, 0, 0, 1]])

        # The same issue applies to the 'scale' parameter
        # which we'll apply to the model, too
        sc = cam[0]
        S = np.array([[sc, 0, 0, 0], [0, sc, 0, 0], [0, 0, sc, 0], [0, 0, 0, 1]])

        if self.tform is None:
            if not self._warned_about_tform:
                logger.warning("Attribute `tform` is not set, so cannot render in the "
                               "original image space, only in cropped image space!")
                self._warned_about_tform = True

            self.tform = np.eye(3)

        # Now we have to do something funky. EMOCA/DECA works on cropped images. This is a problem when
        # we want to quantify motion across frames of a video because a face might move a lot (e.g.,
        # sideways) between frames, but this motion is kind of 'negated' by the crop (which will
        # just yield a cropped image with a face in the middle). Fortunately, the smart people
        # at the MPI encoded the cropping operation as a matrix operation (using a 3x3 similarity
        # transform matrix). So what we'll do (and I can't believe this actually works) is to
        # map the vertices all the way from world space to raster space (in which the crop transform
        # was estimated), then apply the inverse of the crop matrix, and then map it back to world
        # space. To do this, we also need a orthographic projection matrix (OP), which maps from
        # world to NDC space, and a viewport matrix (VP), which maps from NDC to raster space.
        # Note that we need this twice: one for the 'forward' transform (world -> crop raster space)
        # and one for the 'backward' transform (full image raster space -> world)
        OP = create_ortho_matrix(*self._crop_img_size)  # forward (world -> cropped NDC)
        VP = create_viewport_matrix(*self._crop_img_size)  # forward (cropped NDC -> cropped raster)
        CP = crop_matrix_to_3d(self.tform)  # crop matrix
        VP_ = create_viewport_matrix(*self.img_size)  # backward (full NDC -> full raster)
        OP_ = create_ortho_matrix(*self.img_size)  # backward (full NDC -> world)

        # Let's define the *full* transformation chain into a single 4x4 matrix
        # (Order of transformations is from right to left)
        # Again, I can't believe this actually works
        pose = S @ T
        forward = np.linalg.inv(CP) @ VP @ OP
        backward = np.linalg.inv((VP_ @ OP_))
        mat = backward @ forward @ pose

        # Change to homogenous coordinates and apply transformation
        v = np.c_[v, np.ones(v.shape[0])] @ mat.T
        v = v[:, :3]  # trim off 4th dim

        # To complete the full transformation matrix, we need to also
        # add the rotation (which was already applied to the data by the
        # FLAME model)
        mat = mat @ R

        # tex = self.D_flame_tex(enc_dict['tex'])
        return {"v": v, "mat": mat}

    def _world2uv(self, attr, fv):
        batch_size = attr.shape[0]
        uv_attr = self.uv_rasterizer(self.uvcoords.expand(batch_size, -1, -1),
                                     self.uvfaces.expand(batch_size, -1, -1),
                                     fv)[:, :3]
        return uv_attr.detach()

    # def _disp2normal(self, uv_z, v, faces, normals):
    
    #     batch_size = uv_z.shape[0]
    #     fv = face_vertices(v, faces.expand(batch_size, -1, -1))
    #     uv_coarse_vertices = self._world2uv(v, fv)
    #     uv_coarse_normals = self._world2uv(normals, fv)
        
    #     uv_z = uv_z * self.uv_face_eye_mask
    #     uv_detail_vertices = uv_coarse_vertices + uv_z * uv_coarse_normals + \
    #                          self.fixed_uv_dis[None,None,:,:] * \
    #                          uv_coarse_normals.detach()

    #     dense_vertices = uv_detail_vertices.permute(0,2,3,1).reshape([batch_size, -1, 3])
    #     uv_detail_normals = vertex_normals(dense_vertices, self.render.dense_faces.expand(batch_size, -1, -1))
    #     uv_detail_normals = uv_detail_normals.reshape([batch_size, uv_coarse_vertices.shape[2],
    #                                                    uv_coarse_vertices.shape[3], 3])
    #     uv_detail_normals = uv_detail_normals.permute(0,3,1,2)
    #     uv_detail_normals = uv_detail_normals * self.uv_face_eye_mask + \
    #                         uv_coarse_normals * (1. - self.uv_face_eye_mask)
    #     return uv_detail_normals

    def get_faces(self):
        if self.dense:
            return self.dense_template['f']
        else:
            # Cast to cpu and to numpy
            faces = self.faces.cpu().detach().numpy().squeeze()
            return faces

    def __call__(self, image):
        """ Performs reconstruction of the face as a list of landmarks (vertices).

        Parameters
        ----------
        image : torch.Tensor
            A 4D (1 x 3 x 224 x 224) ``torch.Tensor`` representing a RGB image (and a 
            batch dimension of 1); a singleton batch dimension will be added
            automatically if needed

        Returns
        -------
        out : dict
            A dictionary with two keys: ``"v"``, the reconstructed vertices (5023 in 
            total) and ``"mat"``, a 4x4 Numpy array representing the local-to-world
            matrix
        
        Notes
        -----
        Before calling ``__call__``, you *must* set the ``tform`` attribute to the
        estimated cropping matrix (see example below). This is necessary to encode the
        relative position and scale of the bounding box into the reconstructed vertices.    
        
        Examples
        --------
        To reconstruct an example, call the ``EMOCA`` object, but make sure to set the
        ``tform`` attribute first:

        >>> from flame.data import get_example_img
        >>> from flame.crop import CropModel
        >>> img = get_example_img()
        >>> crop_model = CropModel(device='cpu')
        >>> cropped_img = crop_model(img)
        >>> recon_model = FlameReconModel(name='emoca-coarse', device='cpu')
        >>> recon_model.tform = crop_model.tform.params
        >>> out = recon_model(cropped_img)
        >>> out['v'].shape
        (5023, 3)
        >>> out['mat'].shape
        (4, 4)
        """

        image = self._check_input(image, expected_wh=(224, 224))
        enc_dict = self._encode(image)
        dec_dict = self._decode(enc_dict)
        return dec_dict

    def close(self):
        pass