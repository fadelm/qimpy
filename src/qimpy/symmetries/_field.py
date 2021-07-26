import qimpy as qp
import numpy as np
import torch
from typing import Tuple, TYPE_CHECKING
if TYPE_CHECKING:
    from ..grid import Grid, FieldH


class FieldSymmetrizer:
    """Space group symmetrization of reciprocal-space :class:`FieldH`'s."""
    grid: 'Grid'

    def __init__(self, grid: 'Grid') -> None:
        """Initialize symmetrization for fields on `grid`."""
        self.grid = grid
        rc = grid.rc
        shapeH = grid.shapeH
        rot = grid.symmetries.rot.to(torch.long)  # rotations (lattice coords)
        trans = grid.symmetries.trans

        def get_index(iH: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
            """Unique index in half-reciprocal space.
            Also returns whether conjugation was required."""
            iH_wrapped = iH % shapeR
            is_conj = (iH_wrapped[..., 2] >= shapeH[2])  # in redundant half
            iH_wrapped[is_conj] = (-iH_wrapped[is_conj]) % shapeR
            return iH_wrapped @ strideH, is_conj

        # Find symmetry-reduced set:
        iH = grid.get_mesh('H', mine=False).view(-1, 3)  # global mesh
        strideH = torch.tensor([shapeH[1] * shapeH[2], shapeH[2], 1],
                               dtype=torch.long, device=rc.device)
        shapeR = torch.tensor(grid.shape, dtype=torch.long, device=rc.device)
        min_equiv_index = get_index(iH)[0]  # lowest equivalent index
        for rot_i in rot:
            # iH transforms by rot.T, so no transpose on right-multiply:
            min_equiv_index = torch.minimum(min_equiv_index,
                                            get_index(iH @ rot_i)[0])
        iH_reduced = iH[min_equiv_index.unique()]

        # Set up indices and multiplicities of each point in reduced set:
        index, is_conj = get_index((iH_reduced @ rot).transpose(0, 1))
        _, multiplicity = index.unique(sorted=True, return_counts=True)

        if grid.n_procs > 1:
            # Set up 'dest' split over orbits:
            div_dest = qp.utils.TaskDivision(n_tot=iH_reduced.shape[0],
                                             n_procs=grid.n_procs,
                                             i_proc=grid.i_proc)

            # Reduce 'dest' quantities to local subset:
            mine_dest = slice(div_dest.i_start, div_dest.i_stop)
            is_conj = is_conj[mine_dest]
            iH_reduced = iH_reduced[mine_dest]  # to calculate phase below

            # Identify source process of each index ('src' split over grid):
            div_src = grid.split2H
            whose_src = div_src.whose_each(iH[index, 2])

            # Identify what grid data to send from this process and to whom:
            mine_src = torch.where(whose_src == grid.i_proc)
            dest_n_prev = torch.from_numpy(div_dest.n_prev).to(grid.rc.device)
            send_prev = torch.searchsorted(mine_src[0], dest_n_prev)
            iH_local = iH[index[mine_src]] % shapeR  # wrap to positive
            iH_local[..., 2] -= div_src.i_start  # now local H-space index
            strideH_local = torch.tensor(
                [shapeH[1] * grid.shapeH_mine[2], grid.shapeH_mine[2], 1],
                dtype=torch.long, device=rc.device)
            grid_index = iH_local @ strideH_local
            multiplicity = multiplicity.view(
                grid.shapeH)[..., div_src.i_start:div_src.i_stop].flatten()

            # Identify what data is received from each process:
            whose_src_mine = whose_src[mine_dest].flatten()
            local_index = torch.arange(len(whose_src_mine),
                                       device=grid.rc.device)
            recv_index = (whose_src_mine * len(whose_src_mine)
                          + local_index).argsort()  # indices of recv'd data
            recv_prev = torch.searchsorted(whose_src_mine[recv_index],
                                           torch.arange(grid.n_procs + 1,
                                                        device=grid.rc.device))

            # Store required indexing arrays:
            self.send_prev = send_prev.to(grid.rc.cpu).numpy()
            self.recv_prev = recv_prev.to(grid.rc.cpu).numpy()
            self.grid_index = grid_index
            self.recv_index = recv_index
            self.orbit_index = torch.empty_like(local_index)
            self.orbit_index[recv_index] = local_index  # inverse of recv_index

        else:
            self.index = index  # direct grid index by orbits in non-MPI mode

        self.imag_scale = torch.ones_like(is_conj)
        self.imag_scale[is_conj] = -1
        self.phase = qp.utils.cis((-2*np.pi) * (iH_reduced[:, None]
                                                * trans).sum(dim=-1))
        self.n_orbits_mine = is_conj.shape[0]
        self.n_sym = is_conj.shape[1]
        self.inv_multiplicity = (1./self.n_sym) / multiplicity
        qp.log.info(f'Initialized field symmetrization in {len(index)} orbits')

    def __call__(self, v: 'FieldH') -> None:
        """Symmetrize field `v` in-place."""
        grid = self.grid
        watch = qp.utils.StopWatch('FieldH.symmetrize', grid.rc)
        assert v.grid == grid
        n_batch = int(np.prod(v.data.shape[:-3]))
        n_grid = int(np.prod(grid.shapeH_mine))
        v_data = v.data.reshape((n_batch, n_grid))  # flatten batch, grid
        i_batch = torch.arange(n_batch, device=v_data.device)

        watch1 = qp.utils.StopWatch('FieldH.symm_gather', self.grid.rc)

        # Collect data by orbits, transfering over MPI as needed:
        if grid.n_procs > 1:
            # Send data from grid to process containing orbit:
            src_data = v_data[:, self.grid_index].T.contiguous()
            dest_data = torch.empty((len(self.recv_index), n_batch),
                                    dtype=v_data.dtype, device=grid.rc.device)
            mpi_type = grid.rc.mpi_type[v_data.dtype]
            send_counts = np.diff(self.send_prev) * n_batch
            send_offsets = self.send_prev[:-1] * n_batch
            recv_counts = np.diff(self.recv_prev) * n_batch
            recv_offsets = self.recv_prev[:-1] * n_batch
            grid.comm.Alltoallv((qp.utils.BufferView(src_data),
                                 send_counts, send_offsets, mpi_type),
                                (qp.utils.BufferView(dest_data),
                                 recv_counts, recv_offsets, mpi_type))
            # Rearrange data by orbit:
            v_orbits = dest_data[self.orbit_index].T.view(
                n_batch, self.n_orbits_mine, self.n_sym)
        else:
            index = (i_batch[:, None, None], self.index[None])
            v_orbits = v_data[index]

        watch1.stop()
        # Symmetrize in each orbit:
        self._conjugate(v_orbits)

        watch2 = qp.utils.StopWatch('FieldH.symm_perform', self.grid.rc)

        v_sym = torch.einsum('bos, os -> bo', v_orbits, self.phase)
        v_orbits = v_sym[..., None] * self.phase[None].conj()

        watch2.stop()

        self._conjugate(v_orbits)

        watch3 = qp.utils.StopWatch('FieldH.symm_scatter', self.grid.rc)

        # Set results back to original grid, transfering over MPI as needed:
        v_data.zero_()
        if grid.n_procs > 1:
            # Rerrange data and send to process that holds grid point:
            dest_data = v_orbits.flatten(1).T[self.recv_index].contiguous()
            grid.comm.Alltoallv((qp.utils.BufferView(dest_data),
                                 recv_counts, recv_offsets, mpi_type),
                                (qp.utils.BufferView(src_data),
                                 send_counts, send_offsets, mpi_type))
            # Set back to grid (accumulate with all possible rotations):
            v_data.index_put_((i_batch[None], self.grid_index[:, None]),
                              src_data, accumulate=True)
        else:
            v_data.index_put_(index, v_orbits, accumulate=True)

        watch3.stop()

        # Account for multiple accumulated rotations of same grid point:
        v_data *= self.inv_multiplicity[None]
        v.data = v_data.view(v.data.shape)
        watch.stop()

    def _conjugate(self, v_orbits: torch.Tensor) -> None:
        """Conjugate entries of `v_orbits` from Gz < 0 half in-place."""
        watch = qp.utils.StopWatch('FieldH.symm_conj', self.grid.rc)
        v_orbits.imag *= self.imag_scale
        watch.stop()
