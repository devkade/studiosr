import math
from typing import Callable

import torch
import torch.nn as nn

from .base import BaseModule


def default_conv(in_channels, out_channels, kernel_size, bias=True):
    return nn.Conv2d(in_channels, out_channels, kernel_size, padding=(kernel_size // 2), bias=bias)


class MeanShift(nn.Conv2d):
    def __init__(self, rgb_range, rgb_mean=(0.4488, 0.4371, 0.4040), rgb_std=(1.0, 1.0, 1.0), sign=-1):

        super(MeanShift, self).__init__(3, 3, kernel_size=1)
        std = torch.Tensor(rgb_std)
        self.weight.data = torch.eye(3).view(3, 3, 1, 1) / std.view(3, 1, 1, 1)
        self.bias.data = sign * rgb_range * torch.Tensor(rgb_mean) / std
        for p in self.parameters():
            p.requires_grad = False


class Upsampler(nn.Sequential):
    def __init__(self, conv, scale, n_feats, bn=False, act=False, bias=True):

        m = []
        if (scale & (scale - 1)) == 0:  # Is scale = 2^n?
            for _ in range(int(math.log(scale, 2))):
                m.append(conv(n_feats, 4 * n_feats, 3, bias))
                m.append(nn.PixelShuffle(2))
                if bn:
                    m.append(nn.BatchNorm2d(n_feats))
                if act == "relu":
                    m.append(nn.ReLU(True))
                elif act == "prelu":
                    m.append(nn.PReLU(n_feats))

        elif scale == 3:
            m.append(conv(n_feats, 9 * n_feats, 3, bias))
            m.append(nn.PixelShuffle(3))
            if bn:
                m.append(nn.BatchNorm2d(n_feats))
            if act == "relu":
                m.append(nn.ReLU(True))
            elif act == "prelu":
                m.append(nn.PReLU(n_feats))
        else:
            raise NotImplementedError

        super(Upsampler, self).__init__(*m)


# Channel Attention (CA) Layer
class CALayer(nn.Module):
    def __init__(self, channel, reduction=16):
        super(CALayer, self).__init__()
        # global average pooling: feature --> point
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        # feature channel downscale and upscale --> channel weight
        self.conv_du = nn.Sequential(
            nn.Conv2d(channel, channel // reduction, 1, padding=0, bias=True),
            nn.ReLU(inplace=True),
            nn.Conv2d(channel // reduction, channel, 1, padding=0, bias=True),
            nn.Sigmoid(),
        )

    def forward(self, x):
        y = self.avg_pool(x)
        y = self.conv_du(y)
        return x * y


class LAM_Module(nn.Module):
    """Layer attention module"""

    def __init__(self, in_dim):
        super(LAM_Module, self).__init__()
        self.chanel_in = in_dim

        self.gamma = nn.Parameter(torch.zeros(1))
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x):
        """
        inputs :
            x : input feature maps( B X N X C X H X W)
        returns :
            out : attention value + input feature
            attention: B X N X N
        """
        m_batchsize, N, C, height, width = x.size()
        proj_query = x.view(m_batchsize, N, -1)
        proj_key = x.view(m_batchsize, N, -1).permute(0, 2, 1)
        energy = torch.bmm(proj_query, proj_key)
        energy_new = torch.max(energy, -1, keepdim=True)[0].expand_as(energy) - energy
        attention = self.softmax(energy_new)
        proj_value = x.view(m_batchsize, N, -1)

        out = torch.bmm(attention, proj_value)
        out = out.view(m_batchsize, N, C, height, width)

        out = self.gamma * out + x
        out = out.view(m_batchsize, -1, height, width)
        return out


class CSAM_Module(nn.Module):
    """Channel-Spatial attention module"""

    def __init__(self, in_dim):
        super(CSAM_Module, self).__init__()
        self.chanel_in = in_dim

        self.conv = nn.Conv3d(1, 1, 3, 1, 1)
        self.gamma = nn.Parameter(torch.zeros(1))
        # self.softmax  = nn.Softmax(dim=-1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        """
        inputs :
            x : input feature maps( B X N X C X H X W)
        returns :
            out : attention value + input feature
            attention: B X N X N
        """
        m_batchsize, C, height, width = x.size()
        out = x.unsqueeze(1)
        out = self.sigmoid(self.conv(out))

        # proj_query = x.view(m_batchsize, N, -1)
        # proj_key = x.view(m_batchsize, N, -1).permute(0, 2, 1)
        # energy = torch.bmm(proj_query, proj_key)
        # energy_new = torch.max(energy, -1, keepdim=True)[0].expand_as(energy)-energy
        # attention = self.softmax(energy_new)
        # proj_value = x.view(m_batchsize, N, -1)

        # out = torch.bmm(attention, proj_value)
        # out = out.view(m_batchsize, N, C, height, width)

        out = self.gamma * out
        out = out.view(m_batchsize, -1, height, width)
        x = x * out + x
        return x


# Residual Channel Attention Block (RCAB)
class RCAB(nn.Module):
    def __init__(self, conv, n_feat, kernel_size, reduction, bias=True, bn=False, act=nn.ReLU(True), res_scale=1):

        super(RCAB, self).__init__()
        modules_body = []
        for i in range(2):
            modules_body.append(conv(n_feat, n_feat, kernel_size, bias=bias))
            if bn:
                modules_body.append(nn.BatchNorm2d(n_feat))
            if i == 0:
                modules_body.append(act)
        modules_body.append(CALayer(n_feat, reduction))
        self.body = nn.Sequential(*modules_body)
        self.res_scale = res_scale

    def forward(self, x):
        res = self.body(x)
        # res = self.body(x).mul(self.res_scale)
        res += x
        return res


# Residual Group (RG)
class ResidualGroup(nn.Module):
    def __init__(self, conv, n_feat, kernel_size, reduction, act, res_scale, n_resblocks):
        super(ResidualGroup, self).__init__()
        modules_body = []
        modules_body = [
            RCAB(conv, n_feat, kernel_size, reduction, bias=True, bn=False, act=nn.ReLU(True), res_scale=1)
            for _ in range(n_resblocks)
        ]
        modules_body.append(conv(n_feat, n_feat, kernel_size))
        self.body = nn.Sequential(*modules_body)

    def forward(self, x):
        res = self.body(x)
        res += x
        return res


# Holistic Attention Network (HAN)
class HAN(BaseModule):
    def __init__(
        self,
        scale: int = 4,
        n_colors: int = 3,
        img_range: float = 1.0,
        n_resgroups: int = 10,
        n_resblocks: int = 20,
        n_feats: int = 64,
        reduction: int = 16,
        res_scale: float = 1,
        conv: Callable = default_conv,
    ):
        super(HAN, self).__init__()

        kernel_size = 3
        act = nn.ReLU(True)

        # RGB mean for DIV2K
        self.img_range = img_range
        rgb_mean = (0.4488, 0.4371, 0.4040)
        rgb_std = (1.0, 1.0, 1.0)
        self.sub_mean = MeanShift(img_range, rgb_mean, rgb_std)

        # define head module
        modules_head = [conv(n_colors, n_feats, kernel_size)]

        # define body module
        modules_body = [
            ResidualGroup(conv, n_feats, kernel_size, reduction, act=act, res_scale=res_scale, n_resblocks=n_resblocks)
            for _ in range(n_resgroups)
        ]

        modules_body.append(conv(n_feats, n_feats, kernel_size))

        # define tail module
        modules_tail = [Upsampler(conv, scale, n_feats, act=False), conv(n_feats, n_colors, kernel_size)]

        self.add_mean = MeanShift(img_range, rgb_mean, rgb_std, 1)

        self.head = nn.Sequential(*modules_head)
        self.body = nn.Sequential(*modules_body)
        self.csa = CSAM_Module(n_feats)
        self.la = LAM_Module(n_feats)
        self.last_conv = nn.Conv2d(n_feats * 11, n_feats, 3, 1, 1)
        self.last = nn.Conv2d(n_feats * 2, n_feats, 3, 1, 1)
        self.tail = nn.Sequential(*modules_tail)

    def forward(self, x):
        x = self.sub_mean(x)
        x = self.head(x)
        res = x
        # pdb.set_trace()
        for name, midlayer in self.body._modules.items():
            res = midlayer(res)
            # print(name)
            if name == "0":
                res1 = res.unsqueeze(1)
            else:
                res1 = torch.cat([res.unsqueeze(1), res1], 1)
        # res = self.body(x)
        out1 = res
        # res3 = res.unsqueeze(1)
        # res = torch.cat([res1,res3],1)
        res = self.la(res1)
        out2 = self.last_conv(res)

        out1 = self.csa(out1)
        out = torch.cat([out1, out2], 1)
        res = self.last(out)

        res += x
        # res = self.csa(res)

        x = self.tail(res)
        x = self.add_mean(x)
        return x

    @classmethod
    def from_pretrained(
        cls,
        scale: int = 4,
        pretrained: bool = True,
    ):
        import os

        import gdown

        file_ids = {
            2: "12NhWDksOXiVnGw-Zbv6Y20J2DnRRKkJ2",
            3: "1bcos3CfYZ-qfSszxEnPohJaUFgVihOB_",
            4: "1f86ez0hgFLwe9hjhQogHpkACtYgfqrRi",
            8: "1Z5mYsASGKfn77ze25EjS8sUNAJ-KjzpO",
        }
        model = HAN(scale=scale, img_range=255.0)
        if pretrained:
            file_name = f"HAN_BIX{scale}.pt"
            model_dir = "pretrained"
            os.makedirs(model_dir, exist_ok=True)
            file_id = file_ids[scale]
            path = os.path.join(model_dir, file_name)
            if not os.path.exists(path):
                gdown.download(id=file_id, output=path, quiet=False)
            pretrained = torch.load(path)
            model.load_state_dict(pretrained, strict=False)
        return model