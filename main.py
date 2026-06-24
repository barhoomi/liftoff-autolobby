import argparse
import sys
import random
from src.generator import generate_procedural_track
from src.io import save_track_and_race

def main():
    parser = argparse.ArgumentParser(description="Generate a procedural Liftoff track and race.")
    parser.add_argument("--name", type=str, default="Procedural Loop 1", help="Display name of the track")
    parser.add_argument("--id", type=str, default="proc_loop_1", help="Unique ID (folder name) for the track")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for generation")
    parser.add_argument("--points", type=int, default=8, help="Number of spline control points")
    parser.add_argument("--radius", type=float, default=45.0, help="Average radius of the track loop")
    parser.add_argument("--spacing", type=float, default=18.0, help="Approximate spacing between gates in meters")
    parser.add_argument("--elevation", type=float, default=4.0, help="Elevation variance of the track spline")
    parser.add_argument("--laps", type=int, default=3, help="Number of laps for the race")
    parser.add_argument("--env", type=str, default="TheDrawingBoard", help="Liftoff environment name")
    
    args = parser.parse_args()
    
    seed = args.seed if args.seed is not None else random.randint(1, 100000)
    print(f"Generating track '{args.name}' (ID: {args.id}) using seed: {seed}")
    
    try:
        blueprints, checkpoint_ids, spawn_point_id = generate_procedural_track(
            seed=seed,
            num_control_points=args.points,
            radius=args.radius,
            gate_spacing=args.spacing,
            elevation_amplitude=args.elevation
        )
        
        print(f"Placed {len(checkpoint_ids)} checkpoint gates and 1 spawn point.")
        
        track_file, race_file = save_track_and_race(
            track_id=args.id,
            display_name=args.name,
            environment=args.env,
            blueprints=blueprints,
            checkpoint_ids=checkpoint_ids,
            spawn_point_id=spawn_point_id,
            laps=args.laps
        )
        
        print("\nSuccess! Files written successfully:")
        print(f"  Track: {track_file}")
        print(f"  Race:  {race_file}")
        print("\nYou can now open Liftoff, go to Track Builder -> Custom Tracks, or directly launch the race from Single Player -> Classic Race -> Custom Races!")
        
    except Exception as e:
        print(f"Error during track generation: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
