import numpy as np
from typing import List, Optional, Tuple
from deepmd.common import make_default_mesh
from deepmd.infer.data_modifier import DipoleChargeModifier
from deepmd.infer.deep_eval import DeepTensor
import logging

log = logging.getLogger(__name__)


class DeepPot(DeepTensor):
    """Constructor.

    Parameters
    ----------
    model_file : str
        The name of the frozen model file.
    load_prefix: str
        The prefix in the load computational graph
    default_tf_graph : bool
        If uses the default tf graph, otherwise build a new tf graph for evaluation
    """

    def __init__(
        self,
        model_file: str,
        load_prefix: str = "load",
        default_tf_graph: bool = False
    ) -> None:

        # this tensor does not apply here so delete it
        self.tensors.pop("t_sel_type")

        # add these tensors on top of what is defined by DeepTensor Class
        self.tensors.update({
            # general
            "t_dfparam": "fitting_attr/dfparam:0",
            "t_daparam": "fitting_attr/daparam:0",
            # add output tensors
            "t_energy": "o_energy:0",
            "t_force": "o_force:0",
            "t_virial": "o_virial:0",
            "t_ae": "o_atom_energy:0",
            "t_av": "o_atom_virial:0"
        })

        try:
            self._get_tensor("t_efield:0", "t_efield")
            self.has_efield = True
        except KeyError:
            log.debug(f"Could not get tensor 't_efield:0'")
            self.t_efield = None
            self.has_efield = False

        # load optional tensors
        operations = [op.name for op in self.graph.get_operations()]

        if 'load/t_fparam' in operations:
            self.tensors.update({"t_fparam": "t_fparam:0"})
            self.has_fparam = True
        else:
            log.debug(f"Could not get tensor 't_fparam:0'")
            self.t_fparam = None
            self.has_fparam = False

        # check if the graph has aparam
        if 'load/t_aparam' in operations:
            self.tensors.update({"t_aparam": "t_aparam:0"})
            self.has_aparam = True
        else:
            log.debug(f"Could not get tensor 't_aparam:0'")
            self.t_aparam = None
            self.has_aparam = False

        # now when tensors are set initialize DeepTensor which will load them all
        # to class attributes, the run session assciated with the graph
        DeepTensor.__init__(
            self,
            model_file,
            None,
            load_prefix=load_prefix,
            default_tf_graph=default_tf_graph
        )

        # setup modifier
        try:
            t_modifier_type = self._get_tensor("modifier_attr/type:0")
            self.modifier_type = self.sess.run(t_modifier_type).decode("UTF-8")
        except (ValueError, KeyError):
            self.modifier_type = None

        if self.modifier_type == "dipole_charge":
            t_mdl_name = self._get_tensor("modifier_attr/mdl_name:0")
            t_mdl_charge_map = self._get_tensor("modifier_attr/mdl_charge_map:0")
            t_sys_charge_map = self._get_tensor("modifier_attr/sys_charge_map:0")
            t_ewald_h = self._get_tensor("modifier_attr/ewald_h:0")
            t_ewald_beta = self._get_tensor("modifier_attr/ewald_beta:0")
            [mdl_name, mdl_charge_map, sys_charge_map, ewald_h, ewald_beta] = self.sess.run([t_mdl_name, t_mdl_charge_map, t_sys_charge_map, t_ewald_h, t_ewald_beta])
            mdl_charge_map = [int(ii) for ii in mdl_charge_map.decode("UTF-8").split()]
            sys_charge_map = [int(ii) for ii in sys_charge_map.decode("UTF-8").split()]
            self.dm = DipoleChargeModifier(mdl_name, mdl_charge_map, sys_charge_map, ewald_h = ewald_h, ewald_beta = ewald_beta)

    def _run_default_sess(self):
        [self.ntypes, self.rcut, self.dfparam, self.daparam, self.tmap] = self.sess.run(
            [self.t_ntypes, self.t_rcut, self.t_dfparam, self.t_daparam, self.t_tmap]
        )

    def get_sel_type(self) -> List[int]:
        """Unsupported in this model."""
        raise NotImplementedError("This model type does not support this attribute")

    def eval(
        self,
        coords: np.array,
        cells: np.array,
        atom_types: List[int],
        atomic: bool = False,
        fparam: Optional[np.array] = None,
        aparam: Optional[np.array] = None,
        efield: Optional[np.array] = None
    ) -> Tuple[np.ndarray, ...]:
        """Evaluate the energy, force and virial by using this DP.

        Parameters
        ----------
        coords
            The coordinates of atoms.
            The array should be of size nframes x natoms x 3
        cells
            The cell of the region.
            If None then non-PBC is assumed, otherwise using PBC.
            The array should be of size nframes x 9
        atom_types
            The atom types
            The list should contain natoms ints
        atomic
            Calculate the atomic energy and virial
        fparam
            The frame parameter.
            The array can be of size :
            - nframes x dim_fparam.
            - dim_fparam. Then all frames are assumed to be provided with the same fparam.
        aparam
            The atomic parameter
            The array can be of size :
            - nframes x natoms x dim_aparam.
            - natoms x dim_aparam. Then all frames are assumed to be provided with the same aparam.
            - dim_aparam. Then all frames and atoms are provided with the same aparam.
        efield
            The external field on atoms.
            The array should be of size nframes x natoms x 3

        Returns
        -------
        energy
            The system energy.
        force
            The force on each atom
        virial
            The virial
        atom_energy
            The atomic energy. Only returned when atomic == True
        atom_virial
            The atomic virial. Only returned when atomic == True
        """
        if atomic:
            if self.modifier_type is not None:
                raise RuntimeError('modifier does not support atomic modification')
            return self._eval_inner(coords, cells, atom_types, fparam = fparam, aparam = aparam, atomic = atomic, efield = efield)
        else :
            e, f, v = self._eval_inner(coords, cells, atom_types, fparam = fparam, aparam = aparam, atomic = atomic, efield = efield)
            if self.modifier_type is not None:
                me, mf, mv = self.dm.eval(coords, cells, atom_types)
                e += me.reshape(e.shape)
                f += mf.reshape(f.shape)
                v += mv.reshape(v.shape)
            return e, f, v

    def _eval_inner(
        self,
        coords,
        cells,
        atom_types,
        fparam=None,
        aparam=None,
        atomic=False,
        efield=None
    ):
        # standarize the shape of inputs
        atom_types = np.array(atom_types, dtype = int).reshape([-1])
        natoms = atom_types.size
        coords = np.reshape(np.array(coords), [-1, natoms * 3])
        nframes = coords.shape[0]
        if cells is None:
            pbc = False
            # make cells to work around the requirement of pbc
            cells = np.tile(np.eye(3), [nframes, 1]).reshape([nframes, 9])
        else:
            pbc = True
            cells = np.array(cells).reshape([nframes, 9])
        
        if self.has_fparam :
            assert(fparam is not None)
            fparam = np.array(fparam)
        if self.has_aparam :
            assert(aparam is not None)
            aparam = np.array(aparam)
        if self.has_efield :
            assert(efield is not None), "you are using a model with external field, parameter efield should be provided"
            efield = np.array(efield)

        # reshape the inputs 
        if self.has_fparam :
            fdim = self.get_dim_fparam()
            if fparam.size == nframes * fdim :
                fparam = np.reshape(fparam, [nframes, fdim])
            elif fparam.size == fdim :
                fparam = np.tile(fparam.reshape([-1]), [nframes, 1])
            else :
                raise RuntimeError('got wrong size of frame param, should be either %d x %d or %d' % (nframes, fdim, fdim))
        if self.has_aparam :
            fdim = self.get_dim_aparam()
            if aparam.size == nframes * natoms * fdim:
                aparam = np.reshape(aparam, [nframes, natoms * fdim])
            elif aparam.size == natoms * fdim :
                aparam = np.tile(aparam.reshape([-1]), [nframes, 1])
            elif aparam.size == fdim :
                aparam = np.tile(aparam.reshape([-1]), [nframes, natoms])
            else :
                raise RuntimeError('got wrong size of frame param, should be either %d x %d x %d or %d x %d or %d' % (nframes, natoms, fdim, natoms, fdim, fdim))

        # sort inputs
        coords, atom_types, imap = self.sort_input(coords, atom_types)
        if self.has_efield:
            efield = np.reshape(efield, [nframes, natoms, 3])
            efield = efield[:,imap,:]
            efield = np.reshape(efield, [nframes, natoms*3])            

        # make natoms_vec and default_mesh
        natoms_vec = self.make_natoms_vec(atom_types)
        assert(natoms_vec[0] == natoms)

        # evaluate
        energy = []
        force = []
        virial = []
        ae = []
        av = []
        feed_dict_test = {}
        feed_dict_test[self.t_natoms] = natoms_vec
        feed_dict_test[self.t_type  ] = atom_types
        t_out = [self.t_energy, 
                 self.t_force, 
                 self.t_virial]
        if atomic :
            t_out += [self.t_ae, 
                      self.t_av]
        for ii in range(nframes) :
            feed_dict_test[self.t_coord] = np.reshape(coords[ii:ii+1, :], [-1])
            feed_dict_test[self.t_box  ] = np.reshape(cells [ii:ii+1, :], [-1])
            if self.has_efield:
                feed_dict_test[self.t_efield]= np.reshape(efield[ii:ii+1, :], [-1])
            if pbc:
                feed_dict_test[self.t_mesh ] = make_default_mesh(cells[ii:ii+1, :])
            else:
                feed_dict_test[self.t_mesh ] = np.array([], dtype = np.int32)
            if self.has_fparam:
                feed_dict_test[self.t_fparam] = np.reshape(fparam[ii:ii+1, :], [-1])
            if self.has_aparam:
                feed_dict_test[self.t_aparam] = np.reshape(aparam[ii:ii+1, :], [-1])
            v_out = self.sess.run (t_out, feed_dict = feed_dict_test)
            energy.append(v_out[0])
            force .append(v_out[1])
            virial.append(v_out[2])
            if atomic:
                ae.append(v_out[3])
                av.append(v_out[4])

        # reverse map of the outputs
        force  = self.reverse_map(np.reshape(force, [nframes,-1,3]), imap)
        if atomic :
            ae  = self.reverse_map(np.reshape(ae, [nframes,-1,1]), imap)
            av  = self.reverse_map(np.reshape(av, [nframes,-1,9]), imap)

        energy = np.reshape(energy, [nframes, 1])
        force = np.reshape(force, [nframes, natoms, 3])
        virial = np.reshape(virial, [nframes, 9])
        if atomic:
            ae = np.reshape(ae, [nframes, natoms, 1])
            av = np.reshape(av, [nframes, natoms, 9])
            return energy, force, virial, ae, av
        else :
            return energy, force, virial
