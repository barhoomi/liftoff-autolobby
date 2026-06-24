import numpy as np
import math

def catmull_rom_spline(control_points, num_points_per_segment=20, closed=True):
    """
    Interpolates a list of 3D control points using Catmull-Rom splines.
    Args:
        control_points: np.ndarray of shape (N, 3)
        num_points_per_segment: Number of interpolation steps between each control point
        closed: If True, connects the last control point back to the first.
    Returns:
        points: np.ndarray of shape (M, 3)
        tangents: np.ndarray of shape (M, 3) (normalized velocity/tangent vectors)
    """
    N = len(control_points)
    if N < 4:
        raise ValueError("Need at least 4 control points for Catmull-Rom spline.")
        
    pts = []
    tangents = []
    
    if closed:
        # P_{-1} is control_points[-1], P_{N} is control_points[0], P_{N+1} is control_points[1]
        extended_cp = np.zeros((N + 3, 3))
        extended_cp[0] = control_points[-1]
        extended_cp[1:N+1] = control_points
        extended_cp[N+1] = control_points[0]
        extended_cp[N+2] = control_points[1]
        segments = N
    else:
        extended_cp = control_points
        segments = N - 3
        
    for i in range(segments):
        p0 = extended_cp[i]
        p1 = extended_cp[i+1]
        p2 = extended_cp[i+2]
        p3 = extended_cp[i+3]
        
        t_vals = np.linspace(0, 1, num_points_per_segment, endpoint=False)
        for t in t_vals:
            # Catmull-Rom formula:
            # P(t) = 0.5 * ( (2*P1) + (-P0 + P2)*t + (2*P0 - 5*P1 + 4*P2 - P3)*t^2 + (-P0 + 3*P1 - 3*P2 + P3)*t^3 )
            p = 0.5 * (
                (2 * p1) +
                (-p0 + p2) * t +
                (2*p0 - 5*p1 + 4*p2 - p3) * (t**2) +
                (-p0 + 3*p1 - 3*p2 + p3) * (t**3)
            )
            
            # First derivative (tangent vector):
            dp = 0.5 * (
                (-p0 + p2) +
                2 * (2*p0 - 5*p1 + 4*p2 - p3) * t +
                3 * (-p0 + 3*p1 - 3*p2 + p3) * (t**2)
            )
            
            norm = np.linalg.norm(dp)
            dp_norm = dp / norm if norm > 1e-6 else np.array([0.0, 0.0, 1.0])
            
            pts.append(p)
            tangents.append(dp_norm)
            
    return np.array(pts), np.array(tangents)

def get_euler_rotations(tangent):
    """
    Computes Yaw, Pitch, Roll angles (in degrees) for an object aligned with a tangent vector.
    Assumes Liftoff's coordinate system:
    - Yaw (Y-axis rotation): clockwise looking down (XZ plane).
    - Pitch (X-axis rotation): negative values rotate up (climb), positive values rotate down (dive).
    - Roll (Z-axis rotation): set to 0.
    """
    tx, ty, tz = tangent
    
    # Normalize tangent to be safe
    norm = math.sqrt(tx*tx + ty*ty + tz*tz)
    if norm > 1e-6:
        tx, ty, tz = tx/norm, ty/norm, tz/norm
    else:
        tx, ty, tz = 0.0, 0.0, 1.0
        
    # Yaw: angle on XZ plane. Yaw is 0 when facing +Z.
    # Positive Y rotation is clockwise. Z -> X.
    yaw_rad = math.atan2(tx, tz)
    yaw_deg = math.degrees(yaw_rad) % 360.0
    
    # Pitch: angle of climb. Pitch is 0 when horizontal.
    # In Liftoff/Unity, negative pitch rotates nose UP (climbing).
    pitch_rad = math.asin(ty)
    pitch_deg = -math.degrees(pitch_rad)
    
    roll_deg = 0.0
    
    return pitch_deg, yaw_deg, roll_deg
