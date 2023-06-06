from openmm import *
from openmm.app import *
from openmm.unit import *

# Monkey-patch the DCD.writeModel method to avoid writing dummy-atom positions.
writeModel = DCDFile.writeModel
def writeModelPatched(self, positions, unitCellDimensions=None, periodicBoxVectors=None):
    positions = positions[:len(list(self._topology.atoms()))]
    writeModel(self,
               positions,
               unitCellDimensions=unitCellDimensions,
               periodicBoxVectors=periodicBoxVectors)
DCDFile.writeModel = writeModelPatched

# Load the topology and coordinate files.
prmtop = AmberPrmtopFile('test.prm7')
inpcrd = AmberInpcrdFile('test.rst7')

# Initialise the molecular system.
system = prmtop.createSystem(nonbondedMethod=PME,
                             nonbondedCutoff=1*nanometer,
                             constraints=HBonds)

# Restrain the position of atoms using zero-mass dummy atoms.
restraint = HarmonicBondForce()
restraint.setUsesPeriodicBoundaryConditions(True)
system.addForce(restraint)
nonbonded = [f for f in system.getForces() if isinstance(f, NonbondedForce)][0]
dummy_indices = []
positions = inpcrd.positions
restrained_atoms = [1, 4, 5, 6, 8, 10, 14, 15, 16, 18]
for i in restrained_atoms:
    j = system.addParticle(0)
    nonbonded.addParticle(0, 1, 0)
    nonbonded.addException(i, j, 0, 1, 0)
    restraint.addBond(i, j, 0*nanometers, 418400.0*kilojoules_per_mole/nanometer**2)
    dummy_indices.append(j)
    positions.append(positions[i])

# Define the integrator.
integrator = LangevinMiddleIntegrator(300.0*kelvin,
                                1/picosecond,
                                0.002*picoseconds)

# Set the simulation platform.
platform = Platform.getPlatformByName('CPU')
properties = {}

# Initialise and configure the simulation object.
simulation = Simulation(prmtop.topology,
                        system,
                        integrator,
                        platform,
                        properties)
simulation.context.setPositions(positions)
if inpcrd.boxVectors is not None:
    simulation.context.setPeriodicBoxVectors(*inpcrd.boxVectors)

# Setting initial system velocities.
simulation.context.setVelocitiesToTemperature(300.0)

# Add reporters.
simulation.reporters.append(DCDReporter('test.dcd', 100, append=False))
log_file = open('test.log', 'a')
simulation.reporters.append(StateDataReporter(log_file,
                                              100,
                                              step=True,
                                              time=True,
                                              potentialEnergy=True,
                                              kineticEnergy=True,
                                              totalEnergy=True,
                                              temperature=True,
                                              volume=True,
                                              totalSteps=True,
                                              speed=True,
                                              remainingTime=True,
                                              separator=' '))

# Run the simulation.
simulation.step(500)
