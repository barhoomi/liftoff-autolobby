import numpy as np
import random
from src.assets import GATE_BOX_5X5, SPAWN_SINGLE
from src.math_utils import catmull_rom_spline, get_euler_rotations

def generate_procedural_track(
    seed=None,
    num_control_points=8,
    radius=45.0,
    gate_spacing=18.0,
    elevation_mean=8.0,
    elevation_amplitude=4.0
):
    """
    Generates a procedural track loop.
    Returns:
        blueprints: list of dictionaries representing Liftoff objects.
        checkpoint_ids: list of instance IDs corresponding to checkpoint gates.
        spawn_point_id: instance ID of the spawn point.
    """
    if seed is not None:
        np.random.seed(seed)
        random.seed(seed)
        
    # 1. Generate control points in a perturbed circle (loop)
    angles = np.linspace(0, 2 * np.pi, num_control_points, endpoint=False)
    control_points = []
    
    for a in angles:
        # Add random radial perturbation
        r = radius + np.random.uniform(-10.0, 10.0)
        x = r * np.cos(a)
        z = r * np.sin(a)
        
        # Smooth organic elevation changes
        y = elevation_mean + np.random.uniform(-elevation_amplitude, elevation_amplitude)
        # Ensure Y is safely above ground
        y = max(y, 3.0) 
        
        control_points.append([x, y, z])
        
    control_points = np.array(control_points)
    
    # 2. Interpolate using Catmull-Rom spline (dense representation)
    pts, tangents = catmull_rom_spline(control_points, num_points_per_segment=40, closed=True)
    
    # 3. Calculate distance along the spline to sample gates
    diffs = np.diff(pts, axis=0)
    dists = np.linalg.norm(diffs, axis=1)
    cum_dists = np.concatenate(([0.0], np.cumsum(dists)))
    total_length = cum_dists[-1]
    
    # Sample gates along the spline path
    num_gates = int(total_length // gate_spacing)
    sample_dists = np.linspace(0, total_length, num_gates, endpoint=False)
    
    # Compile blueprints list
    blueprints = []
    checkpoint_ids = []
    instance_counter = 1
    
    # 4. Generate gates
    gate_data = []
    for d in sample_dists:
        # Find index in spline
        idx = np.searchsorted(cum_dists, d)
        idx = min(idx, len(pts) - 1)
        
        pos = pts[idx]
        tangent = tangents[idx]
        
        # Calculate pitch, yaw, roll from tangent vector
        pitch, yaw, roll = get_euler_rotations(tangent)
        
        # Enforce minimum height so gates do not clip through the ground
        # CheckpointBox5mX5m01 height is 5m, so center must be >= 2.5m for Y=0 ground.
        if pos[1] < 2.5:
            pos[1] = 2.5
            
        gate_data.append({
            'pos': pos,
            'rot': (pitch, yaw, roll),
            'tangent': tangent
        })
        
    # 5. Add Spawn Point (placed behind the first gate)
    first_gate_pos = gate_data[0]['pos']
    first_gate_tangent = gate_data[0]['tangent']
    first_gate_rot = gate_data[0]['rot'] # (pitch, yaw, roll)
    
    # Spawn position should be ~8 meters behind the first gate along the flight path tangent
    spawn_pos = first_gate_pos - 8.0 * first_gate_tangent
    # Place spawn point resting on the ground
    spawn_pos[1] = 0.1 
    
    spawn_id = instance_counter
    instance_counter += 1
    
    blueprints.append({
        'item_id': SPAWN_SINGLE,
        'instance_id': spawn_id,
        'position': tuple(spawn_pos),
        'rotation': (0.0, first_gate_rot[1], 0.0), # Face the same yaw direction as first gate
        'purpose': 'Functional'
    })
    
    # 6. Add Checkpoint Gates to blueprints
    for gd in gate_data:
        gate_id = instance_counter
        instance_counter += 1
        
        blueprints.append({
            'item_id': GATE_BOX_5X5,
            'instance_id': gate_id,
            'position': tuple(gd['pos']),
            'rotation': gd['rot'],
            'purpose': 'Functional'
        })
        checkpoint_ids.append(gate_id)
        
    return blueprints, checkpoint_ids, spawn_id
