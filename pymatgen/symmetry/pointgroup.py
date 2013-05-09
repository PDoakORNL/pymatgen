#!/usr/bin/env python

"""
This module implements a point group assigner for a collection of atoms.
"""

from __future__ import division

__author__ = "Shyue Ping Ong"
__copyright__ = "Copyright 2012, The Materials Project"
__version__ = "0.1"
__maintainer__ = "Shyue Ping Ong"
__email__ = "shyuep@gmail.com"
__date__ = "5/8/13"

import logging
import itertools
from collections import defaultdict

import numpy as np
try:
    import scipy.cluster as spcluster
except ImportError:
    spcluster = None

from pymatgen.core.operations import SymmOp
from pymatgen.util.coord_utils import find_in_coord_list
from pymatgen.util.decorators import requires

logger = logging.getLogger(__name__)

identity_op = SymmOp(np.eye(4))
inversion_op = SymmOp.inversion()


class PointGroup(list):
    """
    Defines a point group.
    """
    def __init__(self, sch_symbol, operations, tol=0.1):
        """
        Args:
            sch_symbol:
                The schoenflies symbol of the point group.
            operations:
                An initial set of symmetry operations. It is sufficient to
                provide only just enough operations to generate the full set
                of symmetries.
            tol:
                Tolerance to generate the full set of symmetry operations.
        """
        self.sch_symbol = sch_symbol
        super(PointGroup, self).__init__(
            generate_full_symmops(operations, tol))

    def __str__(self):
        return self.sch_symbol

    def __repr__(self):
        return self.__str__()


@requires(spcluster is not None, "Cannot import scipy. PointGroupAnalyzer "
                                 "requires scipy.cluster")
class PointGroupAnalyzer(object):
    """
    A class to analyzer the point group of a molecule.
    """

    def __init__(self, mol, tolerance=0.3, eigen_tolerance=0.01,
                 matrix_tol=0.1):
        """
        Args:
            mol:
                Molecule
            tolerance:
                Distance tolerance to consider sites as symmetrically
                equivalent. Defaults to 0.3 Angstrom.
            eigen_tolerance:
                Tolerance to compare eigen values of the inertia tensor.
                Defaults to 0.01.
            matrix_tol:
                Tolerance used to generate the full set of symmetry
                operations of the point group.
        """
        self.mol = mol
        self.processed_mol = mol.get_centered_molecule()
        self.tol = tolerance
        self.eig_tol = eigen_tolerance
        self.mat_tol = matrix_tol
        self._analyze()

    def _analyze(self):
        if len(self.processed_mol) == 1:
            self.sch_symbol = "Kh"
        else:
            inertia_tensor = np.zeros((3, 3))
            total_inertia = 0
            for site in self.mol:
                x, y, z = site.coords
                wt = site.specie.atomic_mass
                inertia_tensor[0, 0] += wt * (y ** 2 + z ** 2)
                inertia_tensor[1, 1] += wt * (x ** 2 + z ** 2)
                inertia_tensor[2, 2] += wt * (x ** 2 + y ** 2)
                inertia_tensor[0, 1] += -wt * x * y
                inertia_tensor[1, 0] += -wt * x * y
                inertia_tensor[1, 2] += -wt * y * z
                inertia_tensor[2, 1] += -wt * y * z
                inertia_tensor[0, 2] += -wt * x * z
                inertia_tensor[2, 0] += -wt * x * z
                total_inertia += wt * (x ** 2 + y ** 2 + z ** 2)

        # Normalize the inertia tensor so that it does not scale with size of
        # the system.  This mitigates the problem of choosing a proper
        # comparison tolerance for the eigenvalues.
        inertia_tensor /= total_inertia
        eigvals, eigvecs = np.linalg.eig(inertia_tensor)
        self.principal_axes = eigvecs.T
        self.eigvals = eigvals
        v1, v2, v3 = eigvals
        eig_zero = abs(v1 * v2 * v3) < self.eig_tol ** 3
        eig_all_same = abs(v1 - v2) < self.eig_tol and \
                       abs(v1 - v3) < self.eig_tol
        eig_all_diff = abs(v1 - v2) > self.eig_tol and abs(
            v1 - v2) > self.eig_tol and abs(v2 - v3) > self.eig_tol

        self.rot_sym = []
        self.symmops = [identity_op]
        # Separates the Molecule based on the form of its eigenvalues and
        # process accordingly.
        # - Linear molecules have one zero eigenvalue. Possible
        #   symmetry operations are C*v or D*v
        # - Asymetric top molecules have all different eigenvalues. The
        #   maximum rotational symmetry in such molecules is 2
        # - Symmetric top molecules have 1 unique eigenvalue, which gives a
        #   unique rotation axis.  All axial point groups are possible
        #   except the cubic groups (T & O) and I.
        # - Spherical top molecules have all three eigenvalues equal.  They
        #   have the rare T, O or I point groups.  Very difficult to handle,
        #   but rare.
        if eig_zero:
            logger.debug("Linear molecule detected")
            self._proc_linear()
        elif eig_all_same:
            logger.debug("Spherical top molecule detected")
            self._proc_sph_top()
        elif eig_all_diff:
            logger.debug("Asymmetric top molecule detected")
            self._proc_asym_top()
        else:
            logger.debug("Symmetric top molecule detected")
            self._proc_sym_top()

        #     assignedPointGroup = new PointGroup(schSymbol,
        # generateFullSymmetrySet(detectedSymmetries));
        #     log.info("Number of symmetry operations : " +
        #  assignedPointGroup.getOperations().size());
        # }

    def _proc_linear(self):
        if is_valid_op(inversion_op, self.processed_mol, self.tol):
            self.sch_symbol = "D*h"
            self.symmops = [identity_op, inversion_op]
        else:
            self.sch_symbol = "C*v"
            self.symmops = [identity_op]

    def _proc_asym_top(self):
        """
        Handles assymetric top molecules, which cannot contain rotational
        symmetry larger than 2.
        """
        self._check_R2_axes_asym()
        if len(self.rot_sym) == 0:
            logger.debug("No rotation symmetries detected.")
            self._proc_no_rot_sym()
        elif len(self.rot_sym) == 3:
            logger.debug("Dihedral group detected.")
            self._proc_dihedral()
        else:
            logger.debug("Cyclic group detected.")
            self._proc_cyclic()

    def _proc_sym_top(self):
        """
        Handles symetric top molecules which has one unique eigenvalue whose
        corresponding principal axis is a unique rotational axis.  More complex
        handling required to look for R2 axes perpendiarul to this unique axis.
        """
        for i, j in itertools.combinations(xrange(3), 2):
            if abs(self.eigvals[i] - self.eigvals[j]) < self.eig_tol:
                ind = [k for k in xrange(3) if k not in (i, j)][0]
                unique_axis = self.principal_axes[ind]
                break
        self._check_rot_sym(unique_axis)
        if len(self.rot_sym) > 0:
            self._check_perpendicular_r2_axis(unique_axis)

        if len(self.rot_sym) >= 2:
            self._proc_dihedral()
        elif len(self.rot_sym) == 1:
            self._proc_cyclic()
        else:
            self._proc_no_rot_sym()

    def _proc_no_rot_sym(self):
        """
        Handles molecules with no rotational symmetry. Only possible point
        groups are C1, Cs and Ci.
        """
        self.sch_symbol = "C1"
        if is_valid_op(inversion_op, self.processed_mol, self.tol):
            self.sch_symbol = "Ci"
            self.symmops.append(inversion_op)
        else:
            for v in self.principal_axes:
                mirror_type = self._find_mirror(v)
                if not mirror_type == "":
                    self.sch_symbol = "Cs"
                    break

    def _proc_cyclic(self):
        """
        Handles cyclic group molecules.
        """
        main_axis, rot = max(self.rot_sym, key=lambda v: v[1])
        self.sch_symbol = "C{}".format(rot)
        mirror_type = self._find_mirror(main_axis)
        if mirror_type == "h":
            self.sch_symbol += "h"
        elif mirror_type == "v":
            self.sch_symbol += "v"
        elif mirror_type == "":
            if is_valid_op(SymmOp.rotoreflection(main_axis,
                                                 angle=180.0 / rot),
                           self.processed_mol, self.tol):
                self.sch_symbol = "S{}".format(2 * rot)

    def _proc_dihedral(self):
        """
        Handles dihedral group molecules, i.e those with intersecting R2 axes
        and a main axis.
        """
        main_axis, rot = max(self.rot_sym, key=lambda v: v[1])
        self.sch_symbol = "D{}".format(rot)
        mirror_type = self._find_mirror(main_axis)
        if mirror_type == "h":
            self.sch_symbol += "h"
        elif not mirror_type == "":
            self.sch_symbol += "d"

    def _check_R2_axes_asym(self):
        """
        Test for 2-fold rotation along the principal axes. Used to handle
        asymetric top molecules.
        """
        for v in self.principal_axes:
            op = SymmOp.from_origin_axis_angle((0, 0, 0), v, 180)
            if is_valid_op(op, self.processed_mol, self.tol):
                self.symmops.append(op)
                self.rot_sym.append((v, 2))

    def _find_mirror(self, axis):
        """
        Looks for mirror symmetry of specified type about axis.  Possible
        types are "h" or "vd".  Horizontal (h) mirrors are perpendicular to
        the axis while vertical (v) or diagonal (d) mirrors are parallel.  v
        mirrors has atoms lying on the mirror plane while d mirrors do
        not.
        """
        mirror_exists = False
        mirror_type = ""

        #First test whether the axis itself is the normal to a mirror plane.
        if is_valid_op(SymmOp.reflection(axis), self.processed_mol, self.tol):
            self.symmops.append(SymmOp.reflection(axis))
            mirror_type = "h"
        else:
            # Iterate through all pairs of atoms to find mirror
            for s1, s2 in itertools.combinations(self.processed_mol, 2):
                if s1.specie == s2.specie:
                    normal = s1.coords - s2.coords
                    if np.dot(normal, axis) < self.tol:
                        op = SymmOp.reflection(normal)
                        if is_valid_op(op, self.processed_mol, self.tol):
                            self.symmops.append(op)
                            mirror_exists = True
                            break
            if mirror_exists:
                if len(self.rot_sym) > 1:
                    mirror_type = "d"
                    for v, r in self.rot_sym:
                        if not np.linalg.norm(v - axis) < self.tol:
                            if np.dot(v, normal) < self.tol:
                                mirror_type = "v"
                                break
                else:
                    mirror_type = "v"

        return mirror_type

    def _get_smallest_set_not_on_axis(self, axis):
        """
        Returns the smallest list of atoms with the same species and
        distance from origin AND does not lie on the specified axis.  This
        maximal set limits the possible rotational symmetry operations,
        since atoms lying on a test axis is irrelevant in testing rotational
        symmetryOperations.
        """

        def not_on_axis(site):
            v = np.cross(site.coords, axis)
            return np.linalg.norm(v) > self.tol

        valid_sets = []
        origin_site, dist_el_sites = cluster_sites(self.processed_mol, self.tol)
        for test_set in dist_el_sites.values():
            valid_set = filter(not_on_axis, test_set)
            if len(valid_set) > 0:
                valid_sets.append(valid_set)

        return min(valid_sets, key=lambda s: len(s))

    def _check_rot_sym(self, axis):
        """
        Determines the rotational symmetry about supplied axis.  Used only for
        symmetric top molecules which has possible rotational symmetry
        operations > 2.
        """
        min_set = self._get_smallest_set_not_on_axis(axis)
        max_sym = len(min_set)
        for i in xrange(max_sym, 0, -1):
            if max_sym % i != 0:
                continue
            op = SymmOp.from_origin_axis_angle(
                (0, 0, 0), axis, 360 / i)
            rotvalid = is_valid_op(op, self.processed_mol, self.tol)
            if rotvalid:
                self.symmops.append(op)
                self.rot_sym.append((axis, i))
                return i
        return 1

    def _check_perpendicular_r2_axis(self, axis):
        """
        Checks for R2 axes perpendicular to unique axis.  For handling
        symmetric top molecules.
        """
        min_set = self._get_smallest_set_not_on_axis(axis)
        for s1, s2 in itertools.combinations(min_set, 2):
            test_axis = np.cross(s1.coords - s2.coords, axis)
            if np.linalg.norm(test_axis) > self.tol:
                op = SymmOp.from_origin_axis_angle((0, 0, 0), test_axis, 180)
                r2present = is_valid_op(op, self.processed_mol, self.tol)
                if r2present:
                    self.symmops.append(op)
                    self.rot_sym.append((test_axis, 2))
                    return True

    def _proc_sph_top(self):
        """
        Handles Sperhical Top Molecules, which belongs to the T, O or I point
        groups.
        """
        self._find_spherical_axes()
        main_axis, rot = max(self.rot_sym, key=lambda v: v[1])
        if len(self.rot_sym) == 0 or rot < 3:
            logger.debug("Accidental speherical top!")
            self._proc_sym_top()
        elif rot == 3:
            mirror_type = self._find_mirror(main_axis)
            if mirror_type != "":
                if is_valid_op(inversion_op, self.processed_mol, self.tol):
                    self.symmops.append(inversion_op)
                    self.sch_symbol = "Th"
                else:
                    self.sch_symbol = "Td"
            else:
                self.sch_symbol = "T"
        elif rot == 4:
            if is_valid_op(inversion_op, self.processed_mol, self.tol):
                self.symmops.append(inversion_op)
                self.sch_symbol = "Oh"
            else:
                self.sch_symbol = "O"
        elif rot == 5:
            if is_valid_op(inversion_op, self.processed_mol, self.tol):
                self.symmops.append(inversion_op)
                self.sch_symbol = "Ih"
            else:
                self.sch_symbol = "I"

    def _get_smallest_sym_set(self):
        origin_site, dist_el_sites = cluster_sites(self.processed_mol, self.tol)
        return min(dist_el_sites.values(), key=lambda s: len(s))

    def _find_spherical_axes(self):
        """
        Looks for R5, R4, R3 and R2 axes in speherical top molecules.  Point
        group T molecules have only one unique 3-fold and one unique 2-fold
        axis. O molecules have one unique 4, 3 and 2-fold axes. I molecules
        have a unique 5-fold axis.
        """
        rot_present = defaultdict(bool)

        test_set = self._get_smallest_sym_set()
        for s1, s2, s3 in itertools.combinations(test_set, 3):
            if not rot_present[2]:
                test_axis = s1.coords + s2.coords
                if np.linalg.norm(test_axis) > self.tol:
                    op = SymmOp.from_origin_axis_angle((0, 0, 0), test_axis,
                                                       180)
                    rot_present[2] = is_valid_op(op, self.processed_mol,
                                                 self.tol)
                    if rot_present[2]:
                        self.symmops.append(op)
                        self.rot_sym.append((test_axis, 2))
            if not rot_present[2]:
                test_axis = s1.coords + s3.coords
                if np.linalg.norm(test_axis) > self.tol:
                    op = SymmOp.from_origin_axis_angle((0, 0, 0), test_axis,
                                                       180)
                    rot_present[2] = is_valid_op(op, self.processed_mol,
                                                 self.tol)
                    if rot_present[2]:
                        self.symmops.append(op)
                        self.rot_sym.append((test_axis, 2))

            test_axis = np.cross(s2.coords - s1.coords, s3.coords - s1.coords)
            if np.linalg.norm(test_axis) > self.tol:
                for r in (3, 4, 5):
                    if not rot_present[r]:
                        op = SymmOp.from_origin_axis_angle(
                            (0, 0, 0), test_axis, 360/r)
                        rot_present[r] = is_valid_op(
                            op, self.processed_mol, self.tol)
                        if rot_present[r]:
                            self.symmops.append(op)
                            self.rot_sym.append((test_axis, r))
                            break
            if rot_present[2] and rot_present[2] and (
                    rot_present[4] or rot_present[5]):
                break

    def get_pointgroup(self):
        """
        Returns a PointGroup object for the molecule.
        """
        return PointGroup(self.sch_symbol, self.symmops, self.mat_tol)


def is_valid_op(symmop, mol, tol):
    """
    Check if a particular symmetry operation is a valid symmetry operation
    for a molecule, i.e., the operation maps all atoms to another equivalent
    atom.

    Args:
        symmop:
            Symmetry op to test.
        mol:
            Molecule to test. This molecule should be centered with the
            origin at the center of mass.
        tol:
            Absolute tolerance for distance between mapped and actual sites.
    """
    coords = mol.cart_coords
    for site in mol:
        coord = symmop.operate(site.coords)
        ind = find_in_coord_list(coords, coord, tol)
        if not (len(ind) == 1 and
                mol[ind[0]].species_and_occu == site.species_and_occu):
            return False
    return True


def cluster_sites(mol, tol):
    """
    Cluster sites based on distance and species type.

    Args:
        mol:
            Molecule
        tol:
            Tolerance to use.
    """
    # Cluster works for dim > 2 data. We just add a dummy 0 for second
    # coordinate.
    dists = [[np.linalg.norm(site.coords), 0] for site in mol]
    f = spcluster.hierarchy.fclusterdata(dists, tol, criterion='distance')
    clustered_dists = defaultdict(list)
    for i, site in enumerate(mol):
        clustered_dists[f[i]].append(dists[i])
    avg_dist = {label: np.mean(val) for label, val in clustered_dists.items()}
    clustered_sites = defaultdict(list)
    for i, site in enumerate(mol):
        clustered_sites[avg_dist[f[i]]].append(site)

    origin_site = None
    dist_el_sites = {}
    for d, sites in clustered_sites.items():
        if d < tol:
            if len(sites) > 1:
                raise RuntimeError("Bad molecule with more than one atom at "
                                   "origin!")
            else:
                origin_site = sites[0]
        else:
            sites = sorted(sites, key=lambda s: s.specie)
            for sp, g in itertools.groupby(sites, key=lambda s: s.specie):
                dist_el_sites[(d, sp)] = list(g)
    return origin_site, dist_el_sites


def generate_full_symmops(symmops, tol):
    """
    Recursive algorithm to permute through all possible combinations of the
    initially supplied symmetry operations to arrive at a complete set of
    operations mapping a single atom to all other equivalent atoms in the
    point group.  This assumes that the initial number already uniquely
    identifies all operations.

    Args:
        symmops:
            Initial set of symmetry operations.

    Returns:
        Full set of symmetry operations.
    """
    new_set = list(symmops)

    def in_set(op_set, mat):
        for o in op_set:
            if np.allclose(o.affine_matrix, mat, atol=tol):
                return True
        return False

    complete = True

    for op1, op2 in itertools.product(symmops, symmops):
        test_sym = np.dot(op1.affine_matrix, op2.affine_matrix)
        if not in_set(symmops, test_sym):
            new_set.append(SymmOp(test_sym))
            complete = False
            break

    if len(new_set) > 200:
        logger.debug("Generation of symmetry operations in infinite loop.  " +
                     "Possible error in initial operations or tolerance too "
                     "low.")
        return new_set

    if not complete:
        return generate_full_symmops(new_set, tol)
    else:
        return new_set
