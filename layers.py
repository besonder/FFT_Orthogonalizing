import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import einops
import copy
try:
    import sys
    sys.path.append('LConvNet')
    from lconvnet.layers import RKO as _RKO
except:
    pass

# We took BCOP and other Lipschitz-constrained
# layers directly from https://github.com/ColinQiyangLi/LConvNet
from lconvnet.layers import BCOP as _BCOP, \
                            RKO as _RKO, \
                            OSSN as _OSSN, \
                            SVCM as _SVCM, \
                            BjorckLinear

# Extend this class to get emulated striding (for stride 2 only)
class StridedConv(nn.Module):
    def __init__(self, *args, **kwargs):
        striding = False
        if 'stride' in kwargs and kwargs['stride'] == 2:
            args = list(args)
            kwargs['stride'] = 1
            striding = True
            args[0] = 4 * args[0] # 4x in_channels
            if len(args) == 3:
                args[2] = max(1, args[2] // 2) # //2 kernel_size; optional
                kwargs['padding'] = args[2] // 2 # TODO: added maxes recently
            elif 'kernel_size' in kwargs:
                kwargs['kernel_size'] = max(1, kwargs['kernel_size'] // 2)
                kwargs['padding'] = kwargs['kernel_size'] // 2
            args = tuple(args)
        else: # handles OSSN case
            if len(args) == 3:
                kwargs['padding'] = args[2] // 2
            else:
                kwargs['padding'] = kwargs['kernel_size'] // 2
        super().__init__(*args, **kwargs)
        downsample = "b c (w k1) (h k2) -> b (c k1 k2) w h"
        if striding:
            self.register_forward_pre_hook(lambda _, x: \
                    einops.rearrange(x[0], downsample, k1=2, k2=2))  

        
def cayley(W, ED=False):
    if len(W.shape) == 2:
        return cayley(W[None])[0]

    if ED:
        _, cin, cin = W.shape
        I = torch.eye(cin, dtype=W.dtype, device=W.device)[None, :, :]
        A = W - W.conj().transpose(1, 2)
        # print((I+A).shape)
        iIpA = torch.inverse(I + A)
        return iIpA @ (I - A)

    else:
        _, cout, cin = W.shape
        if cin > cout:
            return cayley(W.transpose(1, 2)).transpose(1, 2)
        U, V = W[:, :cin], W[:, cin:]
        I = torch.eye(cin, dtype=W.dtype, device=W.device)[None, :, :]
        A = U - U.conj().transpose(1, 2) + V.conj().transpose(1, 2) @ V
        iIpA = torch.inverse(I + A)
        return torch.cat((iIpA @ (I - A), -2 * V @ iIpA), axis=1)




class CayleyConv(StridedConv, nn.Conv2d):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.register_parameter('alpha', None)

    def fft_shift_matrix(self, n, s):
        shift = torch.arange(0, n).repeat((n, 1))
        shift = shift + shift.T
        return torch.exp(1j * 2 * np.pi * s * shift / n)
    
    def forward(self, x):
        cout, cin, _, _ = self.weight.shape
        batches, _, n, _ = x.shape
        if not hasattr(self, 'shift_matrix'):
            s = (self.weight.shape[2] - 1) // 2
            self.shift_matrix = self.fft_shift_matrix(n, -s)[:, :(n//2 + 1)].reshape(n * (n // 2 + 1), 1, 1).to(x.device)
        xfft = torch.fft.rfft2(x).permute(2, 3, 1, 0).reshape(n * (n // 2 + 1), cin, batches)
        wfft = self.shift_matrix * torch.fft.rfft2(self.weight, (n, n)).reshape(cout, cin, n * (n // 2 + 1)).permute(2, 0, 1).conj()
        if self.alpha is None:
            self.alpha = nn.Parameter(torch.tensor(wfft.norm().item(), requires_grad=True).to(x.device))
        yfft = (cayley(self.alpha * wfft / wfft.norm()) @ xfft).reshape(n, n // 2 + 1, cout, batches)
        y = torch.fft.irfft2(yfft.permute(3, 2, 0, 1))
        if self.bias is not None:
            y += self.bias[:, None, None]
        return y


class CayleyConvED(StridedConv, nn.Conv2d):
    def __init__(self, *args, **kwargs):
        # super().__init__(*args, **kwargs)
        if 'stride' in kwargs and kwargs['stride'] == 2:
            self.stride = 2
            self.xcin = 4*args[0]
            self.padding = 1
            self.k = max(1, args[2]//2)
        else:
            self.stride = 1
            self.xcin = args[0]
            self.padding = 0
            self.k =args[2]
        super().__init__(args[0], self.xcin, self.k, padding=self.padding, stride=self.stride)
        self.bias = nn.Parameter(torch.zeros(args[1]))
        self.args = args
        self.register_parameter('alpha', None)
        self.H = None


    def genH(self, n, k, cout, xcin):
        conv = nn.Conv2d(xcin, cout, k, bias=False)
        s = (conv.weight.shape[2] - 1) // 2
        shift_matrix = self.fft_shift_matrix(n, -s)[:, :(n//2 + 1)].reshape(n * (n // 2 + 1), 1, 1).to(conv.weight.device)
        optimizer = torch.optim.SGD(conv.parameters(), lr=0.1) #lr=0.1
        loss = torch.nn.MSELoss(reduction='mean')
        for i in range(100): #iteration 2000
            H = shift_matrix*torch.fft.rfft2(conv.weight, (n, n)).reshape(cout, xcin, n * (n//2+1)).permute(2, 0, 1).conj()
            b, cout, xcin = H.shape
            Hnorm = torch.norm(H, dim=2)
            
            L1 = loss(Hnorm, torch.ones_like(Hnorm, dtype=Hnorm.dtype)*np.sqrt(xcin/cout))
            
            HHnorm = torch.norm(H, dim=1)
            L2 = loss(HHnorm, torch.ones_like(HHnorm, dtype=HHnorm.dtype))

            if cout >= xcin:
                HH = torch.einsum('npc, nqc -> npq', H.conj(), H)
                M = torch.triu(torch.ones((cout, cout), dtype=bool), diagonal=1).repeat(b, 1, 1).to(H.device)
                HHorth = torch.abs(HH[M])
                L3 = loss(HHorth, torch.zeros_like(HHorth, dtype=HHorth.dtype))
                L = (cout-1)/2*L1 + (cout-1)/2*L2 + L3
            else:
                HH = torch.einsum('ndp, ndq -> npq', H.conj(), H)
                M = torch.triu(torch.ones((xcin, xcin), dtype=bool), diagonal=1).repeat(b, 1, 1).to(H.device)               
                HHorth = torch.abs(HH[M])
                L3 = loss(HHorth, torch.zeros_like(HHorth, dtype=HHorth.dtype))
                L = (xcin-1)/2*L1 + (xcin-1)/2*L2 + L3

            optimizer.zero_grad()
            L.backward()
            optimizer.step()

        H = shift_matrix*torch.fft.rfft2(conv.weight, (n, n)).reshape(cout, xcin, n * (n//2+1)).permute(2, 0, 1).conj()
        self.H = H.to(self.weight.device).detach()


    def fft_shift_matrix(self, n, s):
        shift = torch.arange(0, n).repeat((n, 1))
        shift = shift + shift.T
        return torch.exp(1j * 2 * np.pi * s * shift / n)

    
    def forward(self, x):
        cout = self.args[1]

        batches, _, n, _ = x.shape
        if not hasattr(self, 'shift_matrix'):
            s = (self.weight.shape[2] - 1) // 2
            self.shift_matrix = self.fft_shift_matrix(n, -s)[:, :(n//2 + 1)].reshape(n * (n // 2 + 1), 1, 1).to(x.device)

        xfft = torch.fft.rfft2(x).permute(2, 3, 1, 0).reshape(n * (n // 2 + 1), self.xcin, batches)

        wfft = self.shift_matrix * torch.fft.rfft2(self.weight, (n, n)).reshape(self.xcin, self.xcin, n * (n // 2 + 1)).permute(2, 0, 1).conj()
        if self.alpha is None:
            self.alpha = nn.Parameter(torch.tensor(wfft.norm().item(), requires_grad=True).to(x.device))

        if self.H == None:
            self.genH(n, self.kernel_size[0], cout, self.xcin)

        cwxfft = self.H @ cayley(self.alpha * wfft / wfft.norm(), ED=True) @ xfft

        yfft = (cwxfft).reshape(n, n // 2 + 1, cout, batches)

        y = torch.fft.irfft2(yfft.permute(3, 2, 0, 1))
        if self.bias is not None:
            y += self.bias[:, None, None]
        return y


class CayleyConvED2(StridedConv, nn.Conv2d):
    def __init__(self, *args, **kwargs):
        # super().__init__(*args, **kwargs)
        if 'stride' in kwargs and kwargs['stride'] == 2:
            self.stride = 2
            self.xcin = 4*args[0]
            self.padding = 1
            self.k = max(1, args[2]//2)
        else:
            self.stride = 1
            self.xcin = args[0]
            self.padding = 0
            self.k =args[2]
        super().__init__(args[0], self.xcin, self.k, padding=self.padding, stride=self.stride)
        self.bias = nn.Parameter(torch.zeros(args[1]))
        self.args = args
        self.register_parameter('alpha', None)
        self.H = None


    def genH(self, cout, xcin):
        loss = torch.nn.MSELoss(reduction='mean')
        A = torch.randn(cout, xcin)
        A.requires_grad_(True)
        lr = 0.1
        for i in range(100):
            H = A.detach()
            H.requires_grad_(True)
            Hnorm = torch.norm(H, dim=1)
            L1 = loss(Hnorm, torch.ones_like(Hnorm, dtype=Hnorm.dtype)*np.sqrt(xcin/cout))
            HHnorm = torch.norm(H, dim=0)
            L2 = loss(HHnorm, torch.ones_like(HHnorm, dtype=HHnorm.dtype))
            if cout >= xcin:
                HH =  H @ H.T
                M = torch.triu(torch.ones((cout, cout), dtype=bool), diagonal=1).to(H.device)
                # HHorth = torch.abs(HH[M])
                L3 = loss(HH[M], torch.zeros_like(HH[M], dtype=HH[M].dtype))
                L = (cout-1)/2*L1 + (cout-1)/2*L2 + L3
            else:
                HH =H.T @ H
                M = torch.triu(torch.ones((xcin, xcin), dtype=bool), diagonal=1).repeat(1, 1).to(H.device)               
                # HHorth = torch.abs(HH[M])
                L3 = loss(HH[M], torch.zeros_like(HH[M], dtype=HH[M].dtype))
                L = (xcin-1)/2*L1 + (xcin-1)/2*L2 + L3
            L.backward()
            A = H - lr*H.grad
        H = A[:, :, None, None]
        self.H = H.detach()


    def fft_shift_matrix(self, n, s):
        shift = torch.arange(0, n).repeat((n, 1))
        shift = shift + shift.T
        return torch.exp(1j * 2 * np.pi * s * shift / n)

    
    def forward(self, x):
        cout = self.args[1]

        batches, _, n, _ = x.shape
        if not hasattr(self, 'shift_matrix'):
            s = (self.weight.shape[2] - 1) // 2
            self.shift_matrix = self.fft_shift_matrix(n, -s)[:, :(n//2 + 1)].reshape(n * (n // 2 + 1), 1, 1).to(x.device)

        xfft = torch.fft.rfft2(x).permute(2, 3, 1, 0).reshape(n * (n // 2 + 1), self.xcin, batches)

        wfft = self.shift_matrix * torch.fft.rfft2(self.weight, (n, n)).reshape(self.xcin, self.xcin, n * (n // 2 + 1)).permute(2, 0, 1).conj()
        if self.alpha is None:
            self.alpha = nn.Parameter(torch.tensor(wfft.norm().item(), requires_grad=True).to(x.device))

        wxfft = cayley(self.alpha * wfft / wfft.norm(), ED=True) @ xfft
        yfft = wxfft.reshape(n, n // 2 + 1, self.xcin, batches)
        y = torch.fft.irfft2(yfft.permute(3, 2, 0, 1))
        if self.H == None:
            self.genH(cout, self.xcin)
        y = torch.nn.functional.conv2d(y, self.H.to(x.device))
        if self.bias is not None:
            y += self.bias[:, None, None]
        return y


class CayleyLinear(nn.Linear):
    def __init__(self, in_features, out_features, bias=True):
        super().__init__(in_features, out_features, bias)
        self.alpha = nn.Parameter(torch.ones(1, dtype=torch.float32, requires_grad=True).cuda())
        self.alpha.data = self.weight.norm()

    def reset_parameters(self):
        std = 1 / self.weight.shape[1] ** 0.5
        nn.init.uniform_(self.weight, -std, std)
        if self.bias is not None:
            self.bias.data.uniform_(-std, std)
        self.Q = None
            
    def forward(self, X):
        if self.training or self.Q is None:
            self.Q = cayley(self.alpha * self.weight / self.weight.norm())
        return F.linear(X, self.Q if self.training else self.Q.detach(), self.bias)
    
class BCOP(StridedConv, _BCOP):
    pass

class RKO(StridedConv, _RKO):
    pass

class OSSN(StridedConv, _OSSN):
    pass

class SVCM(StridedConv, _SVCM):
    pass

class PlainConv(nn.Conv2d):
    def forward(self, x):
        if self.kernel_size[0] == 1:
            return super().forward(x)
        if self.kernel_size[0] == 2:
            return super().forward(F.pad(x, (0,1,0,1), mode="circular"))
        return super().forward(F.pad(x, (1,1,1,1)))
    
class GroupSort(nn.Module):
    def forward(self, x):
        a, b = x.split(x.size(1) // 2, 1)
        a, b = torch.max(a, b), torch.min(a, b)
        return torch.cat([a, b], dim=1)
    
class ConvexCombo(nn.Module):
    def __init__(self):
        super().__init__()
        self.alpha = nn.Parameter(torch.Tensor([0.5])) # maybe should be 0.0
        
    def forward(self, x, y):
        s = torch.sigmoid(self.alpha)
        return s * x + (1 - s) * y
    
class Normalize(nn.Module):
    def __init__(self, mu, std):
        super(Normalize, self).__init__()
        self.mu = mu
        self.std = std
        
    def forward(self, x):
        if self.std is not None:
            return (x - self.mu) / self.std
        return (x - self.mu)