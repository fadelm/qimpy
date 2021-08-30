"""Internal interface for XC functionals."""
from __future__ import annotations
from abc import abstractmethod, ABC
import qimpy as qp
import numpy as np
import torch
from functools import lru_cache
from typing import List, Set, Dict

# List exported symbols for doc generation
__all__ = ['Functional', 'LIBXC_AVAILABLE', 'get_libxc_functional_names',
           'FunctionalsLibxc']

try:
    import pylibxc
    LIBXC_AVAILABLE: bool = True  #: Whether Libxc is available.
except ImportError:
    LIBXC_AVAILABLE = False


class Functional(ABC):
    """Abstract base class for exchange-correlation functionals."""
    __slots__ = ('needs_sigma', 'needs_lap', 'needs_tau',
                 'has_exchange', 'has_correlation', 'has_kinetic',
                 'has_energy', 'scale_factor')
    needs_sigma: bool  #: Whether functional needs gradient :math:`\sigma`
    needs_lap: bool  #: Whether functional needs Laplacian :math:`\nabla^2 n`
    needs_tau: bool  #: Whether functional needs KE density :math:`\tau`
    has_exchange: bool  #: Whether functional includes exchange
    has_correlation: bool  #: Whether functional includes correlation
    has_kinetic: bool  #: Whether functional includes kinetic energy
    has_energy: bool  #: Whether functional has meaningful total energy
    scale_factor: float  #: Scale factor in energy and potential

    def __init__(self, *, needs_sigma: bool = False, needs_lap: bool = False,
                 needs_tau: bool = False, has_exchange: bool = False,
                 has_correlation: bool = False, has_kinetic: bool = False,
                 has_energy: bool = True, scale_factor: float = 1.,
                 name: str = '') -> None:
        self.needs_sigma = needs_sigma
        self.needs_lap = needs_lap
        self.needs_tau = needs_tau
        self.has_exchange = has_exchange
        self.has_correlation = has_correlation
        self.has_kinetic = has_kinetic
        self.has_energy = has_energy
        self.scale_factor = scale_factor
        if name:
            scale_str = ('' if (scale_factor == 1.)
                         else f' (scaled by {scale_factor})')
            qp.log.info(f'  {name} functional{scale_str}.')

    @abstractmethod
    def __call__(self, n: torch.Tensor, sigma: torch.Tensor,
                 lap: torch.Tensor, tau: torch.Tensor) -> float:
        """Compute exchange/correlation/kinetic functional for several points.
        The first dimension of each tensor corresponds to spin channels,
        and all subsequent dimenions are grid points.
        Gradients with respect to each input should be accumulated to the
        corresponding `grad` fields (eg. `n.grad`), allowing convenient
        internal use of torch's autograd functionality wherever applicable.

        Parameters
        ----------
        n
            Electron density: 1 or 2 spin channels (up/dn)
        sigma
            Density gradient: 1 or 3 spin channels (up-up, up-dn, dn-dn)
        lap
            Laplacian: 1 or 2 spin channels
        tau
            Kinetic energy density: 1 or 2 spin channels

        Returns
        -------
        Total energy density, summed over all input grid points.
        """


@lru_cache
def get_libxc_functional_names() -> Set[str]:
    """Get set of available Libxc functionals.
    (Empty if Libxc is not available.)"""
    if LIBXC_AVAILABLE:
        return set(pylibxc.util.xc_available_functional_names())
    else:
        return set()


class _FunctionalLibxc:
    """Single Libxc functional.
    Uses a different internal interface than Functional, and is wrapped
    together for all components by FunctionalsLibxc for efficiency."""
    __slots__ = ('functional', 'scale_factor',
                 'needs_sigma', 'needs_lap', 'needs_tau',
                 'has_exchange', 'has_correlation', 'has_kinetic',
                 'has_energy', 'output_labels')
    functional: pylibxc.LibXCFunctional
    scale_factor: float  #: Scale factor in energy and potential
    needs_sigma: bool  #: Whether functional needs gradient :math:`\sigma`
    needs_lap: bool  #: Whether functional needs Laplacian :math:`\nabla^2 n`
    needs_tau: bool  #: Whether functional needs KE density :math:`\tau`
    has_exchange: bool  #: Whether functional includes exchange
    has_correlation: bool  #: Whether functional includes correlation
    has_kinetic: bool  #: Whether functional includes kinetic energy
    has_energy: bool  #: Whether functional has meaningful total energy
    output_labels: List[str]  #: Libxc names of output quantities

    def __init__(self, spin_str: str,
                 name: str, scale_factor: float) -> None:
        func = pylibxc.LibXCFunctional(name, spin_str)
        libxc_flags = pylibxc.functional.flags

        # Check family to determine what inputs are needed:
        self.needs_sigma = False
        self.needs_lap = False
        self.needs_tau = False
        family = func.get_family()
        if family in (libxc_flags.XC_FAMILY_LDA,
                      libxc_flags.XC_FAMILY_HYB_LDA):
            family_str = 'LDA'
        elif family in (libxc_flags.XC_FAMILY_GGA,
                        libxc_flags.XC_FAMILY_HYB_GGA):
            self.needs_sigma = True
            family_str = 'GGA'
        elif family in (libxc_flags.XC_FAMILY_MGGA,
                        libxc_flags.XC_FAMILY_HYB_MGGA):
            self.needs_sigma = True
            self.needs_tau = True
            self.needs_lap = func._needs_laplacian
            family_str = 'MGGA'
        else:
            raise KeyError(f'Unknown Libxc functional family {family}')

        # Check for hybrid functionals:
        if family in (libxc_flags.XC_FAMILY_HYB_LDA,
                      libxc_flags.XC_FAMILY_HYB_GGA,
                      libxc_flags.XC_FAMILY_HYB_MGGA):
            family_str = 'Hybrid ' + family_str
            raise NotImplementedError("Exact exchange / hybrid functionals.")

        # Check kind to determine what components are provided:
        self.has_exchange = False
        self.has_correlation = False
        self.has_kinetic = False
        self.has_energy = func._have_exc
        kind = func.get_kind()
        if kind == libxc_flags.XC_EXCHANGE:
            self.has_exchange = True
            kind_str = "exchange"
        elif kind == libxc_flags.XC_CORRELATION:
            self.has_correlation = True
            kind_str = "correlation"
        elif kind == libxc_flags.XC_EXCHANGE_CORRELATION:
            self.has_exchange = True
            self.has_correlation = True
            kind_str = "exchange-correlation"
        elif kind == libxc_flags.XC_KINETIC:
            self.has_kinetic = True
            kind_str = "KE"
        else:
            raise KeyError(f'Unknown Libxc functional kind {kind}')

        # Store and report functional:
        self.functional = func
        name_str = f'{func.get_name()} {family_str} {kind_str}'
        scale_str = ('' if (scale_factor == 1.)
                     else f' (scaled by {scale_factor})')
        qp.log.info(f'  {name_str} functional from Libxc{scale_str}.')

        # LIst of outputs that will be generated:
        self.output_labels = ['vrho']  # potential always generated
        if self.has_energy:
            self.output_labels.append('zk')
        if self.needs_sigma:
            self.output_labels.append('vsigma')
        if self.needs_lap:
            self.output_labels.append('vlapl')
        if self.needs_tau:
            self.output_labels.append('vtau')

    def __call__(self, inputs: Dict[str, np.ndarray],
                 outputs: Dict[str, np.ndarray]) -> None:
        out = self.functional.compute(inputs,
                                      do_exc=self.has_energy, do_vxc=True)
        # Accumulate results:
        for label in self.output_labels:
            outputs[label] += out[label]


class FunctionalsLibxc(Functional):
    """Evaluate one or more functionals from Libxc together."""
    __slots__ = ('rc', '_functionals')
    rc: qp.utils.RunConfig
    _functionals: List[_FunctionalLibxc]  #: Individual Libxc functionals

    def __init__(self, rc: qp.utils.RunConfig, spin_polarized: bool,
                 libxc_names: Dict[str, float]) -> None:
        """Initialize from Libxc names with scale factors for each."""
        assert LIBXC_AVAILABLE
        spin_str = "polarized" if spin_polarized else "unpolarized"
        self.rc = rc
        self._functionals = [_FunctionalLibxc(spin_str, name, scale_factor)
                             for name, scale_factor in libxc_names.items()]
        # Set combined inputs / components provided:
        super().__init__(
            needs_sigma=any(f.needs_sigma for f in self._functionals),
            needs_lap=any(f.needs_lap for f in self._functionals),
            needs_tau=any(f.needs_tau for f in self._functionals),
            has_exchange=any(f.has_exchange for f in self._functionals),
            has_correlation=any(f.has_correlation for f in self._functionals),
            has_kinetic=any(f.has_kinetic for f in self._functionals),
            has_energy=all(f.has_energy for f in self._functionals))

    def to_xc(self, v: torch.Tensor) -> np.ndarray:
        """Convert data array from internal to XC form."""
        return v.to(self.rc.cpu).flatten(1).T.contiguous().numpy()

    def from_xc(self, v: np.ndarray, v_ref: torch.Tensor) -> torch.Tensor:
        """Convert data array from XC to internal form.
        `v_ref` provides the reference shape for the output."""
        in_shape = v_ref.shape[1:] + v_ref.shape[:1]  # spin dim last in XC
        out = torch.from_numpy(v).contiguous().view(in_shape)
        return out.permute(3, 0, 1, 2).to(self.rc.device)  # spin first now

    def __call__(self, n: torch.Tensor, sigma: torch.Tensor,
                 lap: torch.Tensor, tau: torch.Tensor) -> float:
        # Prepare inputs and empty outputs in LibXC expected form:
        inputs = {'rho': self.to_xc(n)}
        if self.needs_sigma:
            inputs['sigma'] = self.to_xc(sigma)
        if self.needs_lap:
            inputs['lapl'] = self.to_xc(lap)
        if self.needs_tau:
            inputs['tau'] = self.to_xc(tau)

        # Prepare empty outputs in LibXC expected form:
        outputs = {('v'+label): np.zeros_like(data)
                   for label, data in inputs.items()}
        outputs['zk'] = np.zeros((np.prod(n.shape[1:]), 1))  # for energy

        # Compute:
        for functional in self._functionals:
            functional(inputs, outputs)

        # Convert outputs back to internal form:
        e = self.from_xc(outputs['zk'], n[:1])
        n.grad += self.from_xc(outputs['vrho'], n)
        if self.needs_sigma:
            sigma.grad += self.from_xc(outputs['vsigma'], sigma)
        if self.needs_lap:
            lap.grad += self.from_xc(outputs['vlapl'], lap)
        if self.needs_tau:
            tau.grad += self.from_xc(outputs['vtau'], tau)
        return (n * e).sum().item()
