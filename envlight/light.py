import torch
import imageio
from . import renderutils as ru
from .utils import *

def create_trainable_env_rnd(base_res, scale=0.0, bias=0.5, device='cuda'):
    base = torch.rand(
        6, base_res, base_res, 3, 
        dtype=torch.float32, 
        device=device
    ) * scale + bias
    return base

def create_trainable_env_fix(base_res, value, device='cuda'):
    base = torch.full(
        (6, base_res, base_res, 3), 
        value, 
        dtype=torch.float32, 
        device=device
    )
    return base

class EnvLight(torch.nn.Module):

    def __init__(self, path=None, device=None, scale=1.0, min_res=16, max_res=512, min_roughness=0.08, max_roughness=0.5, trainable=False, color_preset=None):
        super().__init__()
        self.device = device if device is not None else 'cuda' # only supports cuda
        self.scale = scale # scale of the hdr values
        self.min_res = min_res # minimum resolution for mip-map
        self.max_res = max_res # maximum resolution for mip-map
        self.min_roughness = min_roughness
        self.max_roughness = max_roughness
        self.trainable = trainable

        if color_preset is not None:
            init_value = create_trainable_env_fix(
                self.max_res, 
                color_preset,
                device=self.device
            )
        else:
            init_value = create_trainable_env_rnd(
                self.max_res,
                device=self.device
            )
            
        # init an empty cubemap
        self.base = torch.nn.Parameter(
            init_value.clone().detach(),
            requires_grad=self.trainable,
        )
        
        # try to load from file
        if path is not None:
            self.load(path)
        
        self.build_mips()
        

    def load(self, path):
        # load latlong env map from file
        image = imageio.imread(path)
        if image.dtype != np.float32:
            image = image.astype(np.float32) / 255

        self.base_image = torch.from_numpy(image).to(self.device) * self.scale
        cubemap = latlong_to_cubemap(self.base_image, [self.max_res, self.max_res], self.device)

        self.base.data = cubemap
        
    def gen_base_image(self):
        self.base_image = cubemap_to_latlong(self.base, [2048, 4096])
        
    def update_light(self, delta):
        self.base_image = torch.roll(self.base_image, delta, dims=1)
        cubemap = latlong_to_cubemap(self.base_image, [self.max_res, self.max_res], self.device)
        self.base.data = cubemap

    def build_mips(self, cutoff=0.99):
        
        self.specular = [self.base]
        while self.specular[-1].shape[1] > self.min_res:
            self.specular += [cubemap_mip.apply(self.specular[-1])]

        self.diffuse = ru.diffuse_cubemap(self.specular[-1])

        for idx in range(len(self.specular) - 1):
            roughness = (idx / (len(self.specular) - 2)) * (self.max_roughness - self.min_roughness) + self.min_roughness
            self.specular[idx] = ru.specular_cubemap(self.specular[idx], roughness, cutoff) 

        self.specular[-1] = ru.specular_cubemap(self.specular[-1], 1.0, cutoff)


    def get_mip(self, roughness):
        # map roughness to mip_level (float):
        # roughness: 0 --> self.min_roughness --> self.max_roughness --> 1
        # mip_level: 0 --> 0                  --> M - 2              --> M - 1
        return torch.where(
            roughness < self.max_roughness, 
            (torch.clamp(roughness, self.min_roughness, self.max_roughness) - self.min_roughness) / (self.max_roughness - self.min_roughness) * (len(self.specular) - 2), 
            (torch.clamp(roughness, self.max_roughness, 1.0) - self.max_roughness) / (1.0 - self.max_roughness) + len(self.specular) - 2
        )
        

    def __call__(self, shading_normal, reflective, roughness):
        # l: [..., 3], normalized direction pointing from shading position to light
        # roughness: [..., 1]
        if self.trainable:
            self.build_mips()
        
        diffuse_light = self._forward_diffuse(shading_normal)
        specular_light = self._forward_specular(reflective, roughness)
        
        return diffuse_light, specular_light
    
    def _forward_diffuse(self, l):

        prefix = l.shape[:-1]
        if len(prefix) != 3: # reshape to [B, H, W, -1]
            l = l.reshape(1, 1, -1, l.shape[-1])
            
        # diffuse light
        light = dr.texture(self.diffuse[None, ...], l.contiguous(), filter_mode='linear', boundary_mode='cube')

        light = light.view(*prefix, -1)
        
        return light
    
    def _forward_specular(self, l, roughness):

        prefix = l.shape[:-1]
        if len(prefix) != 3: # reshape to [B, H, W, -1]
            l = l.reshape(1, 1, -1, l.shape[-1])
            if roughness is not None:
                roughness = roughness.reshape(1, 1, -1, 1)
            
        # specular light
        miplevel = self.get_mip(roughness)
        light = dr.texture(
            self.specular[0][None, ...], 
            l.contiguous(),
            mip=list(m[None, ...] for m in self.specular[1:]), 
            mip_level_bias=miplevel[..., 0], 
            filter_mode='linear-mipmap-linear', 
            boundary_mode='cube'
        )

        light = light.view(*prefix, -1)
        
        return light
    
    def get_env_map(self):
        color = cubemap_to_latlong(self.base, [512, 1024])
        return color
    
    def save_env_map(self, fn):
        color = cubemap_to_latlong(self.base, [512, 1024])
        save_image_raw(fn, color.detach().cpu().numpy())
        