from __future__ import annotations
import qimpy as qp
import numpy as np
import torch


class BasisReal:
    """Extra book-keeping for real basis"""
    __slots__ = ('basis', 'iz0', 'iz0_conj', 'iz0_conj_self',
                 'iz0_mine_local', 'iz0_mine_conj', 'nz0_prev',
                 'Gweight_mine')
    basis: qp.electrons.Basis
    iz0: torch.Tensor  #: Index of Gz = 0 points
    iz0_conj: torch.Tensor  #: Hermitian conjugate points of `iz0`
    iz0_conj_self: torch.Tensor  #: Conjugate indices within Gz = 0 set
    iz0_mine_local: torch.Tensor  #: Local Gz = 0 indices on current process
    iz0_mine_conj: torch.Tensor  #: Global conjugates of `iz0_mine_local`
    nz0_prev: np.ndarray  #: Number of Gz = 0 entries before this process
    Gweight_mine: torch.Tensor  #: Weight of local plane waves

    def __init__(self, basis: qp.electrons.Basis):
        """Initialize extra indexing required for real wavefunctions,
        if needed."""
        assert basis.real_wavefunctions and basis.kpoints.division.n_mine
        self.basis = basis
        div = basis.division
        rc = basis.rc

        # Find conjugate pairs with iG_z = 0:
        iGz = basis.iG[0, :, 2]
        self.iz0 = torch.where(iGz == 0)[0]
        # --- compute index of each point and conjugate in iG_z = 0 plane:
        shapeH = basis.grid.shapeH_mine
        plane_index = basis.fft_index[0, self.iz0].div(shapeH[2],
                                                       rounding_mode='floor')
        iG_conj = (-basis.iG[0, self.iz0, :2]
                   ) % torch.tensor(shapeH[:2], device=rc.device)[None, :]
        plane_index_conj = iG_conj[:, 0] * shapeH[1] + iG_conj[:, 1]
        # --- map plane_index_conj to basis using full plane for look-up:
        plane = torch.zeros(shapeH[0] * shapeH[1], dtype=self.iz0.dtype,
                            device=rc.device)
        plane[plane_index] = self.iz0
        self.iz0_conj = plane[plane_index_conj].clone().detach()
        # --- similar mapping within the Gz = 0 set:
        plane[plane_index] = torch.arange(len(plane_index), device=rc.device)
        self.iz0_conj_self = plane[plane_index_conj].clone().detach()

        # Extract local portions of above:
        mine = torch.where(torch.logical_and(self.iz0 >= div.i_start,
                                             self.iz0 < div.i_stop))[0]
        self.iz0_mine_local = self.iz0[mine] - div.i_start
        self.iz0_mine_conj = self.iz0_conj[mine]
        self.nz0_prev = np.cumsum([0] + rc.comm_b.allgather(len(mine)))

        # Weight by element for overlaps (only for this process portion):
        iGz_mine = iGz[div.i_start:div.i_stop]
        self.Gweight_mine = torch.zeros(div.n_each, device=rc.device)
        self.Gweight_mine[:div.n_mine] = torch.where(iGz_mine == 0, 1., 2.)
        Gweight_sum = qp.utils.globalreduce.sum(self.Gweight_mine, rc.comm_b)
        qp.log.info(f'real basis weight sum: {Gweight_sum:g}')

    def symmetrize(self, coeff: torch.Tensor) -> None:
        """Impose Hermitian symmetry constraint on Gz = 0 coefficients."""
        basis = self.basis

        # Collect all the z0 coefficients:
        is_split = not (coeff.shape[-1] == basis.n_tot)
        if is_split:
            coeff_z0 = torch.empty((self.nz0_prev[-1],) + coeff.shape[:-1],
                                   dtype=coeff.dtype, device=coeff.device)
            coeff_z0_mine = coeff[..., self.iz0_mine_local
                                  ].permute(4, 0, 1, 2, 3  # basis at front
                                            ).contiguous()
            mpi_type = basis.rc.mpi_type[coeff.dtype]
            sendcount = coeff_z0_mine.numel()
            prod_rest = np.prod(coeff.shape[:-1])  # number in all other dims
            recvcounts = np.diff(self.nz0_prev) * prod_rest
            offsets = self.nz0_prev[:-1] * prod_rest
            basis.rc.comm_b.Allgatherv(
                (qp.utils.BufferView(coeff_z0_mine), sendcount, 0, mpi_type),
                (qp.utils.BufferView(coeff_z0), recvcounts, offsets, mpi_type))
            coeff_z0 = coeff_z0.permute(1, 2, 3, 4, 0)  # put basis back at end
        else:  # All coefficients local already:
            coeff_z0 = coeff[..., self.iz0]

        # Symmetrize:
        coeff_z0 = 0.5 * (coeff_z0 + coeff_z0[..., self.iz0_conj_self].conj())

        # Set the symmetrized coefficients:
        if is_split:
            z0_start = self.nz0_prev[basis.rc.i_proc_b]
            z0_stop = self.nz0_prev[basis.rc.i_proc_b + 1]
            coeff[..., self.iz0_mine_local] = coeff_z0[..., z0_start:z0_stop]
        else:
            coeff[..., self.iz0] = coeff_z0
