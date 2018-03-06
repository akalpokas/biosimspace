"""
@package biosimspace
@author  Lester Hedges
@brief   A class for running simulations using AMBER.
"""

from Sire.Base import findExe, Process
from Sire.IO import AmberPrm, AmberRst7, MoleculeParser

from . import process
from ..Protocol.protocol import Protocol, ProtocolType

from math import ceil, floor
from os import chdir, environ, getcwd, path, remove
from re import findall
from shutil import copyfile
from time import sleep
from timeit import default_timer as timer
from warnings import warn

try:
    from Sire import try_import
    pygtail = try_import("pygtail")
except ImportError:
    raise ImportError("Pygtail is not installed. Please install pygtail in order to use BioSimSpace.")

try:
    watchdog = try_import("watchdog")
    from watchdog.events import PatternMatchingEventHandler
    from watchdog.observers import Observer
except ImportError:
    raise ImportError("Watchdog is not installed. Please install watchdog in order to use BioSimSpace.")

try:
    mdtraj = try_import("mdtraj")
except ImportError:
    raise ImportError("MDTraj is not installed. Please install mdtraj in order to use BioSimSpace.")

try:
    mdanalysis = try_import("MDAnalysis")
except ImportError:
    raise ImportError("MDAnalysis is not installed. Please install mdanalysis in order to use BioSimSpace.")

class Watcher:
    """A class to watch for changes to the AMBER energy info file. An event handler
       is used trigger updates to the energy dictionary each time the file is modified.
    """
    def __init__(self, proc):
        """Constructor.

           Positional arguments:

           proc -- The Amber Process object.
        """
        self._process = proc

        self._observer = Observer()
        self._process = proc

    def start(self):
        """Start the file watcher."""

        # Setup the event handler and observer.
        event_handler = Handler(self._process)
        self._observer.schedule(event_handler, self._process._work_dir)
        self._observer.daemon = True
        self._observer.start()

class Handler(PatternMatchingEventHandler):
    """An event handler to trigger updates to the energy dictionary each time
       the log file is changed.
    """

    # Overwrite defaults.
    case_sensitive = False
    ignore_directories = True
    ignore_patterns = []
    patterns = "*.nrg"

    def __init__(self, proc):
        """Constructor.

           Positional arguments:

           proc -- The Amber Process object.
        """
        self._process = proc

    def on_any_event(self, event):
        """Update the dictionary when the file is modified.

           Positional arguments:

           event -- The file system event.
        """

        # N.B.
        #
        # Since the watchdog package is cross-platform it doesn't support
        # detection of "close-write" operations, so multiple "modified" events
        # can be triggered while the log file is being written. As such, we
        # check to see if the file has been updated by seeing whether the
        # NSTEP record is different to the most recent entry in the dictionary.
        # So far, no issues have been found with processing partially written
        # files, i.e. duplicate or missing records.

        if event.event_type == 'modified':
            # If this is the first time the file has been modified since the
            # process started, then wipe the dictionary and flag that the file
            # is now being watched.
            if not self._process._is_watching:
                self._process._stdout_dict = process.MultiDict()
                self._process._is_watching = True

            # Now update the dictionary with any new records.
            self._process._update_energy_dict()

class Amber(process.Process):
    """A class for running simulations using AMBER."""

    def __init__(self, system, protocol, exe=None, name="amber",
            work_dir=None, seed=None):
        """Constructor.

           Positional arguments:

           system   -- The molecular system.
           protocol -- The protocol for the AMBER process.

           Keyword arguments:

           exe      -- The full path to the AMBER executable.
           name     -- The name of the process.
           work_dir -- The working directory for the process.
           seed     -- A random number seed.
        """

        # Call the base class constructor.
        super().__init__(system, protocol, name, work_dir, seed)

        # If the path to the executable wasn't specified, then search
        # for it in $PATH. For now, we'll just search for 'sander', which
        # is available free as part of AmberTools. In future, we will
        # look for all possible executables in order of preference: pmemd.cuda,
        # pmemd, sander, etc., as well as their variants, e.g. pmemd.MPI.
        if exe is None:
            # Search AMBERHOME, if set.
            if "AMBERHOME" in environ:
                amber_home = environ.get("AMBERHOME")
                if path.isfile("%s/bin/sander" % amber_home):
                    self._exe = "%s/bin/sander" % amber_home

            # Search PATH.
            else:
                self._exe = findExe("sander").absoluteFilePath()

        else:
            # Make sure executable exists.
            if path.isfile(exe):
                self._exe = protocol
            else:
                raise IOError(('AMBER executable doesn\'t exist: "{x}"').format(x=exe))

        # Initialise the energy dictionary and header.
        self._stdout_dict = process.MultiDict()

        # Create the name of the energy output file and wipe the
        # contents of any existing file.
        self._nrg_file = "%s/%s.nrg" % (self._work_dir, name)
        open(self._nrg_file, 'w').close()

        # Initialise the energy watcher.
        self._watcher = None
        self._is_watching = False

        # The names of the input files.
        self._rst_file = "%s/%s.rst7" % (self._work_dir, name)
        self._prm_file = "%s/%s.prm7" % (self._work_dir, name)

        # Set the path for the AMBER configuration file.
        # The 'protocol' argument may contain the path to a custom file.

        # Set the config file name.
        if not self._is_custom:
            self._config_file = "%s/%s.amber" % (self._work_dir, name)

        # The user has supplied a custom config file.
        else:
            # Make sure the file exists.
            if path.isfile(protocol):
                self._config_file = protocol
            else:
                raise IOError(('AMBER configuration file doesn\'t exist: "{x}"').format(x=config_file))

        # Create the list of input files.
        self._input_files = [self._config_file, self._rst_file, self._prm_file]

        # Now set up the working directory for the process.
        self._setup()

    def _setup(self):
        """Setup the input files and working directory ready for simulation."""

        # Create the input files...

        # RST file (coordinates).
        rst = AmberRst7(self._system)
        rst.writeToFile(self._rst_file)

        # PRM file (topology).
        prm = AmberPrm(self._system)
        prm.writeToFile(self._prm_file)

        # Generate the AMBER configuration file.
        # Skip if the user has passed a custom config.
        if not self._is_custom:
            self._generate_config()
            self.writeConfig(self._config_file)

        # Generate the dictionary of command-line arguments.
        self._generate_args()

        # Return the list of input files.
        return self._input_files

    def _generate_config(self):
        """Generate AMBER configuration file strings."""

        # Clear the existing configuration list.
        self._config = []

        # Check whether the system contains periodic box information.
        # For now, well not attempt to generate a box if the system property
        # is missing. If no box is present, we'll assume a non-periodic simulation.
        if 'space' in self._system.propertyKeys():
            if self._protocol.gas_phase:
                has_box = False
            else:
                has_box = True
        else:
            has_box = False

        # Add configuration variables for a minimisation simulation.
        if self._protocol.type() == ProtocolType.MINIMISATION:
            self.addToConfig("Minimisation")
            self.addToConfig(" &cntrl")
            self.addToConfig("  imin=1,")                   # Minisation simulation.
            self.addToConfig("  ntx=1,")                    # Only read coordinates from file.
            self.addToConfig("  ntxo=1,")                   # Output coordinates in ASCII.
            self.addToConfig("  ntpr=100,")                 # Output energies every 100 steps.
            self.addToConfig("  irest=0,")                  # Don't restart.
            self.addToConfig("  maxcyc=%d,"
                % self._protocol.steps)                     # Set the number of steps.
            if not has_box:
                self.addToConfig("  ntb=0,")                # No periodic box.
                self.addToConfig("  cut=999.,")             # Non-bonded cut-off.
            else:
                self.addToConfig("  cut=8.0,")              # Non-bonded cut-off.
            self.addToConfig(" /")

        # Add configuration variables for an equilibration simulation.
        elif self._protocol.type() == ProtocolType.EQUILIBRATION:
            # Work out the number of integration steps.
            steps = ceil(self._protocol.runtime / 2e-6)

            # Set the random number seed.
            if self._is_seeded:
                seed = self._seed
            else:
                seed = -1

            self.addToConfig("Equilibration.")
            self.addToConfig(" &cntrl")
            self.addToConfig("  ig=%d," % seed)             # Random number seed.
            self.addToConfig("  ntx=1,")                    # Only read coordinates from file.
            self.addToConfig("  ntxo=1,")                   # Output coordinates in ASCII.
            self.addToConfig("  ntpr=100,")                 # Output energies every 100 steps.
            self.addToConfig("  ntwr=500,")                 # Save restart configuration every 500 steps.
            self.addToConfig("  irest=0,")                  # Don't restart.
            self.addToConfig("  dt=0.002,")                 # Time step (2fs).
            self.addToConfig("  nstlim=%d," % steps)        # Number of integration steps.
            self.addToConfig("  ntc=2,")                    # Enable SHAKE.
            self.addToConfig("  ntf=2,")                    # Don't calculate forces for constrained bonds.
            self.addToConfig("  ntt=3,")                    # Langevin dynamics.
            self.addToConfig("  gamma_ln=2,")               # Collision frequency (ps).
            if not has_box:
                self.addToConfig("  ntb=0,")                # No periodic box.
                self.addToConfig("  cut=999.,")             # Non-bonded cut-off.
            else:
                self.addToConfig("  cut=8.0,")              # Non-bonded cut-off.
            self.addToConfig("  ntp=1,")                    # Isotropic pressure scaling.
            self.addToConfig("  pres0=1.01325,")            # Atompspheric pressure.

            # Restrain the backbone.
            if self._protocol.is_restrained:
                self.addToConfig("  ntr=1,")
                self.addToConfig("  restraint_wt=2,")
                self.addToConfig("  restraintmask='@CA,C,O,N',")

            # Heating/cooling protocol.
            if not self._protocol.isConstantTemp():
                self.addToConfig("  tempi=%.2f," % self._protocol.temperature_start)
                self.addToConfig("  temp0=%.2f," % self._protocol.temperature_end)
                self.addToConfig("  nmropt=1,")
                self.addToConfig(" /")
                self.addToConfig("&wt TYPE='TEMP0', istep1=0, istep2=%d, value1=%.2f, value2=%.2f /"
                    % (steps, self._protocol.temperature_start, self._protocol.temperature_end))
                self.addToConfig("&wt TYPE='END' /")

            # Constant temperature equilibration.
            else:
                self.addToConfig("  temp0=%.2f," % self._protocol.temperature_start)
                self.addToConfig(" /")

        # Add configuration variables for a production simulation.
        elif self._protocol.type() == ProtocolType.PRODUCTION:
            # Work out the number of integration steps.
            steps = ceil(self._protocol.runtime / 2e-6)

            # Set the random number seed.
            if self._seed is None:
                seed = -1
            else:
                seed = self._seed

            self.addToConfig("Production.")
            self.addToConfig(" &cntrl")
            self.addToConfig("  ig=%d," % seed)             # Random number seed.
            if self._protocol.restart:
                self.addToConfig("  ntx=5,")                # Read coordinates and velocities.
            else:
                self.addToConfig("  ntx=1,")                # Only read coordinates.
            self.addToConfig("  ntxo=1,")                   # Output coordinates in ASCII.
            self.addToConfig("  ntpr=100,")                 # Output energies every 100 steps.
            self.addToConfig("  ntwr=500,")                 # Save restart configuration every 500 steps.
            self.addToConfig("  ntwx=%d,"                   # Trajectory sampling frequency.
                % floor(steps / self._protocol.frames))
            if self._protocol.restart:
                self.addToConfig("  irest=1,")              # Restart using previous velocities.
            else:
                self.addToConfig("  irest=0,")              # Don't restart.
            self.addToConfig("  dt=0.002,")                 # Time step (2fs).
            self.addToConfig("  nstlim=%d," % steps)        # Number of integration steps.
            self.addToConfig("  ntc=2,")                    # Enable SHAKE.
            self.addToConfig("  ntf=2,")                    # Don't calculate forces for constrained bonds.
            self.addToConfig("  ntt=3,")                    # Langevin dynamics.
            self.addToConfig("  gamma_ln=2,")               # Collision frequency (ps).
            if not has_box:
                self.addToConfig("  ntb=0,")                # No periodic box.
                self.addToConfig("  cut=999.,")             # Non-bonded cut-off.
            else:
                self.addToConfig("  cut=8.0,")              # Non-bonded cut-off.
            if not self._protocol.restart:
                self.addToConfig("  tempi=%.2f,"            # Initial temperature.
                    % self._protocol.temperature)
            self.addToConfig("  temp0=%.2f,"                # Target temperature.
                % self._protocol.temperature)

            # Constant pressure control.
            if self._protocol.ensemble is 'NPT':
                self.addToConfig("  ntp=1,")                # Isotropic pressure scaling.
                self.addToConfig("  pres0=1.01325,")        # Atompspheric pressure.

            self.addToConfig(" /")

    def _generate_args(self):
        """Generate the dictionary of command-line arguments."""

        # Clear the existing arguments.
        self.clearArgs()

        # Add the default arguments.
        self.setArg("-O", True)                             # Overwrite.
        self.setArg("-i", "%s.amber" % self._name)          # Input file.
        self.setArg("-p", "%s.prm7" % self._name)           # Topology file.
        self.setArg("-c", "%s.rst7" % self._name)           # Coordinate file.
        self.setArg("-o", "stdout")                         # Redirect to stdout.
        self.setArg("-r", "%s.crd" % self._name)            # Restart file.
        self.setArg("-inf", "%s.nrg" % self._name)          # Energy info file.

        # Skip if the user has passed a custom config.
        if not self._is_custom:

            # Append a reference file if this a constrained equilibration.
            if self._protocol.type() == ProtocolType.EQUILIBRATION:
                if self._protocol.is_restrained:
                    self.setArg("-ref", "%s.rst7" % self._name)

            # Append a trajectory file if this is a production run.
            elif self._protocol.type() == ProtocolType.PRODUCTION:
                self.setArg("-x", "%s.nc" % self._name)

    def start(self):
        """Start the AMBER simulation. """

        # Process is already running.
        if self._process is not None:
            if self._process.isRunning():
                return

        # Reset the watcher.
        self._is_watching = False

        # Store the current working directory.
        dir = getcwd()

        # Change to the working directory for the process.
        # This avoid problems with relative paths.
        chdir(self._work_dir)

        # Create the arguments string list.
        args = self.getArgStringList()

        # Write the command-line process to a README.txt file.
        with open("README.txt", "w") as f:

            # Set the command-line string.
            self._command = "%s " % self._exe + self.getArgString()

            # Write the command to file.
            f.write("# AMBER was run with the following command:\n")
            f.write("%s\n" % self._command)

        # Start the timer.
        self._timer = timer()

        # Start the simulation.
        self._process = Process.run(self._exe, args,
            "%s.out"  % self._name, "%s.err"  % self._name)

        # Change back to the original working directory.
        chdir(dir)

	# Watch the energy info file for changes.
        self._watcher = Watcher(self)
        self._watcher.start()

    def getSystem(self):
        """Get the latest molecular configuration as a Sire system."""

        # Create the name of the restart CRD file.
        restart = "%s/%s.crd" % (self._work_dir, self._name)

        # Check that the file exists.
        if path.isfile(restart):
            # Create and return the molecular system.
            return MoleculeParser.read(restart, self._prm_file)

        else:
            return None

    def getTrajectory(self, format='mdtraj'):
        """Get the current trajectory object.

           Keyword arguments:

           format -- Whether to return a 'MDTraj' or 'MDAnalysis' object.
        """

        if format.upper() not in ['MDTRAJ', 'MDANALYSIS']:
            warn("Invalid trajectory format. Using default (mdtraj).")
            format = mdtraj

        # Check for a trajectory file.
        has_traj = False
        if path.isfile("%s/%s.nc" % (self._work_dir, self._name)):
            traj_file = "%s/%s.nc" % (self._work_dir, self._name)
            has_traj = True

        # We found a trajectory file.
        if has_traj:

            # Return an MDTraj object.
            if format is 'mdtraj':

                # MDTraj currently doesn't support the .prm7 extension, so we
                # need to copy the topology file to a temporary .parm7 file.
                # I've submitted a pull request to add support for the alternative
                # .prm7 extension.
                top_file = "%s/tmp.parm7" % self._work_dir
                copyfile(self._prm_file, top_file)

                # Create the MDTraj object.
                traj = mdtraj.load(traj_file, top=top_file)

                # Delete the temporary .parm7 file.
                remove("%s/tmp.parm7" % self._work_dir)

                return traj

            # Return an MDAnalysis Universe.
            else:
                return mdanalysis.Universe(self._prm_file, traj_file, topology_format='PARM7')

        else:
            return None

    def getFrames(self, indices=None):
        """Get trajectory frames as a list of Sire systems.

           Keyword arguments:

           indices -- A list of trajectory frame indices.
        """

        # First get the current MDTraj object.
        traj = self.getTrajectory()

        # There is no trajectory.
        if traj is None:
            return None

        # Create the indices array.

        # Default to all frames.
        if indices is None:
            indices = [x for x in range(0, traj.n_frames)]

        # Insert the single frame into a list.
        elif type(indices) is int:
            indices = [indices]

        # Make sure all entries are of type int.
        elif all(isinstance(item, int) for item in indices):
            pass

        # Unsupported argument.
        else:
            raise ValueError("Unsupported index. Indices must be an 'int', or list of 'int' types.")

        # Intialise the list of frames.
        frames = []

        # Store the maximum frame number.
        max_frame = traj.n_frames - 1

        # Loop over all indices.
        for x in indices:

            # Make sure the frame index is within range.
            if abs(x) > max_frame:
                raise ValueError("Frame index (%d) of of range (0-%d)." %s (x, max_frame))

            # The name of the frame coordinate file.
            frame_file = "%s/frame.nc" % self._work_dir

            # Write the current frame as a NetCDF file.
            # MDTraj is unable to write a CRD file that is compatible with the original topology!
            traj[x].save(frame_file)

            # Create a Sire system.
            system = MoleculeParser.read([self._prm_file, frame_file])

            # Append the system to the list of frames.
            frames.append(system)

        # Remove the temporary frame coordinate file.
        remove(frame_file)

        # Return the frames.
        return frames

    def nFrames(self):
        """Return the current number of trajectory frames."""

        # First get the current MDTraj object.
        traj = self.getTrajectory()

        # There is no trajectory.
        if traj is None:
            return 0

        else:
            return traj.n_frames

    def getRecord(self, record, time_series=False):
        """Get a record from the stdout dictionary.

           Positional arguments:

           record      -- The record key.

           Keyword arguments:

           time_series -- Whether to return a list of time series records.
        """
        self._update_energy_dict()
        return self._get_stdout_record(record.strip().upper(), time_series)

    def getRecords(self):
        """Return the dictionary of stdout time-series records."""
        return self._stdout_dict

    def getTime(self, time_series=False):
        """Get the time (in nanoseconds)."""

        # No time records for minimisation protocols.
        if self._protocol.type() == ProtocolType.MINIMISATION:
            return None

        self._update_energy_dict()

        # Get the list of time steps.
        time_steps = self._get_stdout_record('TIME(PS)', time_series)

        # Convert from picoseconds to nanoseconds.
        if time_steps is not None:
            if time_series:
                return [x * 1e-3 for x in time_steps]
            else:
                return 1e-3 * time_steps

    def getStep(self, time_series=False):
        """Get the number of integration steps."""
        self._update_energy_dict()
        return self._get_stdout_record('NSTEP', time_series)

    def getBondEnergy(self, time_series=False):
        """Get the bond energy."""
        self._update_energy_dict()
        return self._get_stdout_record('BOND', time_series)

    def getAngleEnergy(self, time_series=False):
        """Get the angle energy."""
        self._update_energy_dict()
        return self._get_stdout_record('ANGLE', time_series)

    def getDihedralEnergy(self, time_series=False):
        """Get the dihedral energy."""
        self._update_energy_dict()
        return self._get_stdout_record('DIHED', time_series)

    def getElectrostaticEnergy(self, time_series=False):
        """Get the electrostatic energy."""
        self._update_energy_dict()
        return self._get_stdout_record('EELECT', time_series)

    def getElectrostaticEnergy14(self, time_series=False):
        """Get the electrostatic energy between atoms 1 and 4."""
        self._update_energy_dict()
        return self._get_stdout_record('1-4 EEL', time_series)

    def getVanDerWaalsEnergy(self, time_series=False):
        """Get the Van der Vaals energy."""
        self._update_energy_dict()
        return self._get_stdout_record('VDWAALS', time_series)

    def getHydrogenBondEnergy(self, time_series=False):
        """Get the Van der Vaals energy."""
        self._update_energy_dict()
        return self._get_stdout_record('EHBOND', time_series)

    def getRestraintEnergy(self, time_series=False):
        """Get the restraint energy."""
        self._update_energy_dict()
        return self._get_stdout_record('RESTRAINT', time_series)

    def getPotentialEnergy(self, time_series=False):
        """Get the potential energy."""
        self._update_energy_dict()
        return self._get_stdout_record('EPTOT', time_series)

    def getKineticEnergy(self, time_series=False):
        """Get the kinetic energy."""
        self._update_energy_dict()
        return self._get_stdout_record('EKTOT', time_series)

    def getNonBondedEnergy14(self, time_series=False):
        """Get the non-bonded energy between atoms 1 and 4."""
        self._update_energy_dict()
        return self._get_stdout_record('1-4 NB', time_series)

    def getTotalEnergy(self, time_series=False):
        """Get the total energy."""
        self._update_energy_dict()

        if self._protocol.type() == ProtocolType.MINIMISATION:
            return self._get_stdout_record('ENERGY', time_series)
        else:
            return self._get_stdout_record('ETOT', time_series)

    def getCentreOfMassKineticEnergy(self, time_series=False):
        """Get the kinetic energy of the centre of mass in translation."""
        self._update_energy_dict()
        return self._get_stdout_record('EKCMT', time_series)

    def getVirial(self, time_series=False):
        """Get the virial."""
        self._update_energy_dict()
        return self._get_stdout_record('VIRIAL', time_series)

    def getTemperature(self, time_series=False):
        """Get the temperature."""
        self._update_energy_dict()
        return self._get_stdout_record('TEMP(K)', time_series)

    def getPressure(self, time_series=False):
        """Get the temperature."""
        self._update_energy_dict()
        return self._get_stdout_record('PRESS', time_series)

    def getVolume(self, time_series=False):
        """Get the volume."""
        self._update_energy_dict()
        return self._get_stdout_record('VOLUME', time_series)

    def getDensity(self, time_series=False):
        """Get the density."""
        self._update_energy_dict()
        return self._get_stdout_record('DENSITY', time_series)

    def _update_energy_dict(self):
        """Read the energy info file and update the dictionary."""

        # Flag that this isn't a header line.
        is_header = False

        # Open the file for reading.
        with open(self._nrg_file, "r") as f:

            # Loop over all of the lines.
            for line in f:

                # Skip empty lines and summary reports.
                if len(line) > 0 and line[0] is not '|':

                    # The output format is different for minimisation protocols.
                    if self._protocol.type() == ProtocolType.MINIMISATION:

                        # No equals sign in the line.
                        if "=" not in line:

                            # Split the line using whitespace.
                            data = line.upper().split()

                            # If we find a header, jump to the top of the loop.
                            if len(data) > 0:
                                if data[0] == 'NSTEP':
                                    is_header = True
                                    continue

                        # Process the header record.
                        if is_header:

                            # Split the line using whitespace.
                            data = line.upper().split()

                            # The file hasn't been updated.
                            if 'NSTEP' in self._stdout_dict and data[0] == self._stdout_dict['NSTEP'][-1]:
                                return

                            else:
                                # Add the timestep and energy records to the dictionary.
                                self._stdout_dict['NSTEP'] = data[0]
                                self._stdout_dict['ENERGY'] = data[1]

                                # Turn off the header flag now that the data has been recorded.
                                is_header = False

                    # All other protocols have output that is formatted as RECORD = VALUE.

                    # Use a regex search to split the line into record names and values.
                    records = findall("(\d*\-*\d*\s*[A-Z]+\(*[A-Z]*\)*)\s*=\s*(\-*\d+\.?\d*)", line.upper())

                    # Append each record to the dictionary.
                    for key, value in records:

                        # Strip whitespace from the record key.
                        key = key.strip()

                        # The file hasn't been updated.
                        if key == 'NSTEP':
                            if 'NSTEP' in self._stdout_dict and value == self._stdout_dict['NSTEP'][-1]:
                                return
                            else:
                                self._stdout_dict[key] = value
                        else:
                            self._stdout_dict[key] = value

    def kill(self):
        """Kill the running process."""

        # Stop and join the watchdog observer.
        if self._watcher is not None:
            self._watcher._observer.stop()
            self._watcher._observer.join()

        # Kill the process.
        self._process.kill()

    def wait(self):
        """Wait for the process to finish."""

        # Loop until the process is finished.
        # For some reason we can't use Sire.Base.Process.wait() since it
        # doesn't work properly with the background threads used for the
        # watchdog observer.
        while self._process.isRunning():
            sleep(1)

        # Stop and join the watchdog observer.
        self._watcher._observer.stop()
        self._watcher._observer.join()

    def _get_stdout_record(self, key, time_series=False):
        """Helper function to get a stdout record from the dictionary.

           Positional arguments:

           key        -- The record key.

           Keyword arguments:

           time_series -- Whether to return a time series of records.
        """

        # No data!
        if len(self._stdout_dict) is 0:
            return None

        if type(time_series) is not bool:
            warn("Non-boolean time-series flag. Defaulting to False!")
            time_series = False

        # Return the list of dictionary values.
        if time_series:
            try:
                if key is 'NSTEP':
                    return [int(x) for x in self._stdout_dict[key]]
                else:
                    return [float(x) for x in self._stdout_dict[key]]

            except KeyError:
                return None

        # Return the most recent dictionary value.
        else:
            try:
                if key is 'NSTEP':
                    return int(self._stdout_dict[key][-1])
                else:
                    return float(self._stdout_dict[key][-1])

            except KeyError:
                return None
