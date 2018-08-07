######################################################################
# BioSimSpace: Making biomolecular simulation a breeze!
#
# Copyright: 2017-2018
#
# Authors: Lester Hedges <lester.hedges@gmail.com>
#
# BioSimSpace is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# BioSimSpace is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with BioSimSpace. If not, see <http://www.gnu.org/licenses/>.
#####################################################################

"""
Functionality for aligning molecules.
Author: Lester Hedges <lester.hedges@gmail.com>
"""

import Sire.Maths as _SireMaths
import Sire.Mol as _SireMol

from .._SireWrappers import MergedMolecule as _MergedMolecule
from .._SireWrappers import Molecule as _Molecule

import BioSimSpace.Units as _Units

def matchAtoms(molecule0,
               molecule1,
               scoring_function="RMSD align",
               matches=1,
               return_scores=False,
               prematch={},
               timeout=5*_Units.Time.second,
               match_light=True,
               map0={},
               map1={},
               verbose=False):
    """Find mappings between atom indices in molecule0 to those in molecule1.
       Molecules are aligned using a Maximum Common Substructure (MCS) search.
       When requesting more than one match, the mappings will be sorted using
       a scoring function and returned in order of best to worst score. (Note
       that, depending on the scoring function the "best" score may have the
       lowest value.)

       Positional arguments
       --------------------

       molecule0 : BioSimSpace._SireWrappers.Molecule
           The reference molecule.

       molecule1 : BioSimSpace._SireWrappers.Molecule
           The target molecule.


       Keyword arguments
       -----------------

       scoring_function : str
           The scoring function used to match atoms. Available options are:
             - "RMSD"
                 Calculate the root mean squared distance between the
                 coordinates of atoms in molecule0 to those that they
                 map to in molecule1.
             - "RMSD align"
                 Align molecule0 to molecule1 based on the mapping before
                 computing the above RMSD score.

       matches : int
           The maximum number of matches to return. (Sorted in order of score).

       return_scores : bool
           Whether to return a list containing the scores for each mapping.

       prematch : dict
           A pre-match to use as the basis of the search.

       timeout : BioSimSpace.Types.Time
           The timeout for the matching algorithm.

       match_light : bool
           Whether to match light atoms.

       map0 : dict
           A dictionary that maps "properties" in molecule0 to their user
           defined values. This allows the user to refer to properties
           with their own naming scheme, e.g. { "charge" : "my-charge" }

       map1 : dict
           A dictionary that maps "properties" in molecule1 to their user
           defined values.

       verbose : bool
           Whether to print status information from the matcher.


       Returns
       -------

       matches : dict, [dict], ([dict], list)
           The best atom mapping, a list containing a user specified number of
           the best mappings ranked by their score, or a tuple containing the
           list of best mappings and a list of the corresponding scores.
    """

    # A list of supported scoring functions.
    scoring_functions = ["RMSD", "RMSDALIGN"]

    # Validate input.

    if type(molecule0) is not _Molecule:
        raise TypeError("'molecule0' must be of type 'BioSimSpace._SireWrappers.Molecule'")

    if type(molecule1) is not _Molecule:
        raise TypeError("'molecule1' must be of type 'BioSimSpace._SireWrappers.Molecule'")

    if type(scoring_function) is not str:
        raise TypeError("'scoring_function' must be of type 'str'")
    else:
        # Strip whitespace and convert to upper case.
        scoring_function = scoring_function.replace(" ", "").upper()
        if not scoring_function in scoring_functions:
            raise ValueError("Unsupported scoring function '%s'. Options are: %s"
                % (scoring_function, scoring_functions))

    if type(matches) is not int:
        raise TypeError("'matches' must be of type 'int'")
    else:
        if matches < 0:
            raise ValueError("'matches' must be positive!")

    if type(prematch) is not dict:
        raise TypeError("'prematch' must be of type 'dict'")
    else:
        for idx0, idx1 in prematch.items():
            if type(idx0) is not _SireMol.AtomIdx or type(idx1) is not _SireMol.AtomIdx:
                raise TypeError("'prematch' dictionary key:value pairs must be of type 'Sire.Mol.AtomIdx'")

    if type(timeout) is not _Units.Time._Time:
        raise TypeError("'timeout' must be of type 'BioSimSpace.Types.Time'")

    if type(match_light) is not bool:
        raise TypeError("'match_light' must be of type 'bool'")

    if type(map0) is not dict:
        raise TypeError("'map0' must be of type 'dict'")

    if type(map1) is not dict:
        raise TypeError("'map1' must be of type 'dict'")

    if type(verbose) is not bool:
        raise TypeError("'verbose' must be of type 'bool'")

    # Extract the Sire molecule from each BioSimSpace molecule.
    mol0 = molecule0._getSireMolecule()
    mol1 = molecule1._getSireMolecule()

    # Convert the timeout to a Sire unit.
    timeout = timeout.magnitude() * timeout._supported_units[timeout.unit()]

    # Are we performing an alignment before scoring.
    if scoring_function == "RMSDALIGN":
        is_align = True
    else:
        is_align = False

    # Find all of the best maximum common substructure matches.
    mappings = ( mol0.evaluate()
                     .findMCSmatches(mol1, _SireMol.AtomResultMatcher(prematch),
                         timeout, match_light, map0, map1, verbose)
               )

    # No matches!
    if len(mappings) == 0:
        return None

    # Score the mappings and return them in sorted order (best to worst).
    # For now we default to RMSD scoring, since it's the only option.
    else:
        # Return the best match.
        if matches == 1:
            return _score_rmsd(mol0, mol1, mappings, is_align)[0][0]
        else:
            # Return a list of matches from best to worst.
            if return_scores:
                (mappings, scores) = _score_rmsd(mol0, mol1, mappings, is_align)
                return (mappings[0:matches], scores[0:matches])
            # Return a tuple containing the list of matches from best to
            # worst along with the list of scores.
            else:
                return _score_rmsd(mol0, mol1, mappings, is_align)[0][0:matches]

def rmsdAlign(molecule0, molecule1, mapping=None, map0={}, map1={}):
    """Align atoms in molecule0 to those in molecule1 using the mapping
       between matched atom indices. The molecule is aligned based on
       a root mean squared displacement (RMSD) fit to find the optimal
       translation vector (as opposed to merely taking the difference of
       centroids).

       Positional arguments
       --------------------

       molecule0 : BioSimSpace._SireWrappers.Molecule
           The reference molecule.

       molecule1 : BioSimSpace._SireWrappers.Molecule
           The target molecule.


       Keyword arguments
       -----------------

       mapping : dict
           A dictionary mapping atoms in molecule0 to those in molecule1.

       map0 : dict
           A dictionary that maps "properties" in molecule0 to their user
           defined values. This allows the user to refer to properties
           with their own naming scheme, e.g. { "charge" : "my-charge" }

       map1 : dict
           A dictionary that maps "properties" in molecule1 to their user
           defined values.


       Returns
       -------

       molecule : BioSimSpace._SireWrappers.Molecule
           The aligned molecule.
    """

    if type(molecule0) is not _Molecule:
        raise TypeError("'molecule0' must be of type 'BioSimSpace._SireWrappers.Molecule'")

    if type(molecule1) is not _Molecule:
        raise TypeError("'molecule1' must be of type 'BioSimSpace._SireWrappers.Molecule'")

    if type(map0) is not dict:
        raise TypeError("'map0' must be of type 'dict'")

    if type(map1) is not dict:
        raise TypeError("'map1' must be of type 'dict'")

    # The user has passed an atom mapping.
    if mapping is not None:
        if type(mapping) is not dict:
            raise TypeError("'mapping' must be of type 'dict'.")
        else:
            # Make sure all key/value pairs are of type AtomIdx.
            for idx0, idx1 in mapping.items():
                if type(idx0) is not _SireMol.AtomIdx or type(idx1) is not _SireMol.AtomIdx:
                    raise TypeError("key:value pairs in 'mapping' must be of type 'Sire.Mol.AtomIdx'")

    # Get the best match atom mapping.
    else:
        mapping = matchAtoms(molecule0, molecule1, map0=map0, map1=map1)

    # Extract the Sire molecule from each BioSimSpace molecule.
    mol0 = molecule0._getSireMolecule()
    mol1 = molecule1._getSireMolecule()

    # Perform the alignment, mol0 to mol1.
    mol0 = mol0.move().align(mol1, _SireMol.AtomResultMatcher(mapping)).molecule()

    # Return the aligned molecule.
    return _Molecule(mol0)

def merge(molecule0, molecule1, mapping=None, map0={}, map1={}):
    """Create a merged molecule from 'molecule0' and 'molecule1' based on the
       atom index 'mapping'. The merged molecule can be used in single- and
       dual-toplogy free energy calculations.

       molecule0 : BioSimSpace._SireWrappers.Molecule
           A molecule object.

       molecule1 : BioSimSpace._SireWrappers.Molecule
           A second molecule object.


       Keyword arguments
       -----------------

       mapping : dict
           The mapping between matching atom indices in the two molecules.
           If no mapping is provided, then atoms in molecule0 will be mapped
           to those in molecule1 using "findMCSmatches", with "rmsdAlign" then
           used to align molecule0 to molecule1 based on the resulting mapping.

       map0 : dict
           A dictionary that maps "properties" in molecule0 to their user
           defined values. This allows the user to refer to properties
           with their own naming scheme, e.g. { "charge" : "my-charge" }

       map1 : dict
           A dictionary that maps "properties" in molecule1 to their user
           defined values.
    """

    if type(molecule0) is not _Molecule:
        raise TypeError("'molecule0' must be of type 'BioSimSpace._SireWrappers.Molecule'")

    if type(molecule1) is not _Molecule:
        raise TypeError("'molecule1' must be of type 'BioSimSpace._SireWrappers.Molecule'")

    if type(map0) is not dict:
        raise TypeError("'map0' must be of type 'dict'")

    if type(map1) is not dict:
        raise TypeError("'map1' must be of type 'dict'")

    # The user has passed an atom mapping.
    if mapping is not None:
        if type(mapping) is not dict:
            raise TypeError("'mapping' must be of type 'dict'.")
        else:
            # Make sure all key/value pairs are of type AtomIdx.
            for idx0, idx1 in mapping.items():
                if type(idx0) is not _SireMol.AtomIdx or type(idx1) is not _SireMol.AtomIdx:
                    raise TypeError("key:value pairs in 'mapping' must be of type 'Sire.Mol.AtomIdx'")

    # Get the best atom mapping and align molecule0 to molecule1 based on the
    # mapping.
    else:
        mapping = matchAtoms(molecule0, molecule1, map0=map0, map1=map1)
        molecule0 = rmsdAlign(molecule0, molecule1, mapping)

    # Create and return the merged molecule.
    return _MergedMolecule(molecule0, molecule1, mapping, map0=map0, map1=map1)

def _score_rmsd(molecule0, molecule1, mappings, is_align=False):
    """Internal function to score atom mappings based on the root mean squared
       displacement (RMSD) between mapped atoms in two molecules. Optionally,
       molecule0 can first be aligned to molecule1 based on the mapping prior
       to computing the RMSD. The function returns the mappings sorted based
       on their score from best to worst, along with a list containing the
       scores for each mapping.

       Positional arguments
       --------------------

       molecule0 : BioSimSpace._SireWrappers.Molecule
           The reference molecule.

       molecule1 : BioSimSpace._SireWrappers.Molecule
           The target molecule.

       mappings : [ dict ]
           A list of dictionaries mapping  atoms in molecule0 to those in
           molecule1.

       is_align : bool
           Whether to align molecule0 to molecule1 based on the mapping
           before calculating the RMSD score.


       Returns
       -------

       mappinps : [ dict ]
           The sorted mappings.
    """

    # Initialise a list of scores.
    scores = []

    # Loop over all mappings.
    for map in mappings:
        # Align molecule0 to molecule1 based on the mapping.
        if is_align:
            molecule0 = molecule0.move().align(molecule1, _SireMol.AtomResultMatcher(map)).molecule()

        # We now compute the RMSD between the coordinates of the matched atoms
        # in molecule0 and molecule1.

        # Initialise lists to hold the coordinates.
        c0 = []
        c1 = []

        # Loop over each atom index in the map.
        for idx0, idx1 in map.items():
            # Append the coordinates of the matched atom in molecule0.
            c0.append(molecule0.atom(idx0).property("coordinates"))
            # Append the coordinates of atom in molecule1 to which it maps.
            c1.append(molecule1.atom(idx1).property("coordinates"))

        # Compute the RMSD between the two sets of coordinates.
        scores.append(_SireMaths.getRMSD(c0, c1))

    # Sort the scores and return the sorted keys. (Smaller RMSD is best)
    keys = sorted(range(len(scores)), key=lambda k: scores[k])

    # Sort the mappings.
    mappings = [mappings[x] for x in keys]

    # Sort the scores and convert to Angstroms.
    scores = [scores[x] * _Units.Length.angstrom for x in keys]

    # Return the sorted mappings and their scores.
    return (mappings, scores)