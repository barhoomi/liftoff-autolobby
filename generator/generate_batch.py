import subprocess
import sys
import os
import random

PRESETS = [
    {"shape": "circle", "points": 8, "radius": 45.0, "elevation": 4.0},
    {"shape": "triangle", "points": 6, "radius": 55.0, "elevation": 6.0},
    {"shape": "square", "points": 10, "radius": 40.0, "elevation": 3.0},
    {"shape": "circle", "points": 12, "radius": 50.0, "elevation": 7.0},
    {"shape": "triangle", "points": 8, "radius": 35.0, "elevation": 2.0},
    {"shape": "square", "points": 6, "radius": 60.0, "elevation": 5.0},
    {"shape": "circle", "points": 10, "radius": 45.0, "elevation": 6.0},
    {"shape": "triangle", "points": 12, "radius": 55.0, "elevation": 4.0},
    {"shape": "square", "points": 8, "radius": 50.0, "elevation": 7.0},
    {"shape": "circle", "points": 8, "radius": 40.0, "elevation": 5.0},
]

def main():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    main_py = os.path.join(script_dir, "main.py")
    
    print(f"[Batch] Starting generation of {len(PRESETS)} tracks...")
    
    for i, preset in enumerate(PRESETS, 1):
        track_id = f"proc_batch_{i}"
        track_name = f"Procedural Batch {i}"
        seed = random.randint(1, 100000)
        
        cmd = [
            sys.executable,
            main_py,
            "--name", track_name,
            "--id", track_id,
            "--seed", str(seed),
            "--shape", preset["shape"],
            "--points", str(preset["points"]),
            "--radius", str(preset["radius"]),
            "--elevation", str(preset["elevation"]),
            "--publish"
        ]
        
        print(f"\n[Batch] [{i}/{len(PRESETS)}] Generating {track_name} (Seed: {seed}, Shape: {preset['shape']})...")
        print(f"[Batch] Executing: {' '.join(cmd)}")
        
        try:
            # Run the command and let stdout pass through to the console
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"[Batch] ERROR: Preset {i} failed to generate or publish: {e}", file=sys.stderr)
            sys.exit(1)
            
    print("\n[Batch] SUCCESS! Generated and published all 10 tracks successfully.")

if __name__ == "__main__":
    main()
