import argparse
import sys
import random
import os
from src.generator import generate_procedural_track
from src.io import save_track_and_race


def extract_track_name_from_rotation_line(line):
    """Extract just the track-name field from one ``tracks_to_rotate.txt`` line
    ("TrackName, Environment, GameMode"), rightmost-split into exactly 3 fields so a
    comma inside the track name itself (e.g. the real Liftoff track "Iceberg, Right
    ahead!") is preserved verbatim instead of shearing at the first comma. Identical to
    a plain ``split(",")[0]`` for a line with <=3 fields, so every existing
    (comma-free-name) file behaves the same as before.
    See docs/features/doing/bug-comma-in-track-name.md.
    """
    parts = line.split(",")
    if len(parts) <= 3:
        return parts[0].strip() if parts else ""
    return ",".join(parts[:-2]).strip()


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
    parser.add_argument("--shape", type=str, default="circle", choices=["circle", "triangle", "square"], help="Shape of the track path")
    parser.add_argument("--publish", action="store_true", help="Publish the generated track directly to Steam Workshop")
    
    args = parser.parse_args()
    
    seed = args.seed if args.seed is not None else random.randint(1, 100000)
    print(f"Generating track '{args.name}' (ID: {args.id}) using seed: {seed}")
    
    try:
        blueprints, checkpoint_ids, spawn_point_id = generate_procedural_track(
            seed=seed,
            num_control_points=args.points,
            radius=args.radius,
            gate_spacing=args.spacing,
            elevation_amplitude=args.elevation,
            shape=args.shape
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
        
        if args.publish:
            print("\n[Publish] Starting Steam Workshop publish sequence...")
            from src.publish import publish_track
            publish_track(args.id)

        # Update rotation configuration file
        try:
            import json
            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "lobby_config.json")
            if os.path.exists(config_path):
                with open(config_path, "r") as f:
                    config = json.load(f)
                liftoff_path = config.get("liftoff_path")
                if liftoff_path:
                    if not os.path.exists(liftoff_path):
                        import getpass
                        current_user = getpass.getuser()
                        parts = liftoff_path.split("/")
                        if len(parts) > 2 and parts[1] == "home":
                            parts[2] = current_user
                            alternative_path = "/".join(parts)
                            if os.path.exists(alternative_path):
                                liftoff_path = alternative_path
                                print(f"[Rotation] Auto-corrected liftoff path to current user's home: {liftoff_path}")
                    game_dir = os.path.dirname(liftoff_path)
                    plugins_dir = os.path.join(game_dir, "BepInEx", "plugins")
                    os.makedirs(plugins_dir, exist_ok=True)
                    
                    tracks_file = os.path.join(plugins_dir, "tracks_to_rotate.txt")
                    
                    # Read existing tracks to prevent duplicates.
                    existing_tracks = []
                    if os.path.exists(tracks_file):
                        with open(tracks_file, "r") as f:
                            for line in f:
                                stripped = line.strip()
                                if stripped and not stripped.startswith("#"):
                                    existing_tracks.append(extract_track_name_from_rotation_line(stripped))
                                    
                    # If this track is not in rotation, append it!
                    if args.name not in existing_tracks:
                        env_ui = args.env
                        if env_ui == "TheDrawingBoard":
                            env_ui = "The Drawing Board"
                        
                        with open(tracks_file, "a") as f:
                            f.write(f"{args.name},{env_ui},Infinite Race\n")
                        print(f"[Rotation] Added track '{args.name}' to rotation file: {tracks_file}")
        except Exception as e:
            print(f"[Rotation] WARNING: Failed to update tracks_to_rotate.txt: {e}")
        
    except Exception as e:
        print(f"Error during track generation: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
