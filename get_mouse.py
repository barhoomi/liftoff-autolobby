import time
import subprocess
import re
import sys

def get_mouse_position():
    try:
        # Get active window ID
        out = subprocess.check_output(["xdotool", "getactivewindow"]).decode().strip()
        # Get window name
        name = subprocess.check_output(["xdotool", "getwindowname", out]).decode().strip()
        # Get mouse location
        loc = subprocess.check_output(["xdotool", "getmouselocation", "--shell"]).decode().strip()
        x = re.search(r"X=(\d+)", loc).group(1)
        y = re.search(r"Y=(\d+)", loc).group(1)
        
        # Get window geometry
        geom = subprocess.check_output(["xdotool", "getwindowgeometry", out]).decode().strip()
        match_pos = re.search(r"Position: (\d+),(\d+)", geom)
        match_geo = re.search(r"Geometry: (\d+)x(\d+)", geom)
        
        if match_pos and match_geo:
            rel_x = int(x) - int(match_pos.group(1))
            rel_y = int(y) - int(match_pos.group(2))
            width = int(match_geo.group(1))
            height = int(match_geo.group(2))
            
            # Avoid division by zero
            width = max(width, 1)
            height = max(height, 1)
            
            pct_x = rel_x / width
            pct_y = rel_y / height
            return name, rel_x, rel_y, pct_x, pct_y
    except Exception as e:
        pass
    return None

def main():
    # Check if xdotool is installed
    try:
        subprocess.run(["xdotool", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        print("ERROR: xdotool is not installed. Please run: sudo apt install xdotool")
        sys.exit(1)

    print("="*80)
    print("MOUSE COORDINATE HELPER")
    print("="*80)
    print("Instructions:")
    print("1. Open Liftoff in windowed mode.")
    print("2. Hover your mouse over the menus (Multiplayer, Create Room, etc.).")
    print("3. Copy the PERCENTAGE coordinates to lobby_config.json for resolution-independence.")
    print("Press Ctrl+C to stop.\n")
    
    try:
        last_coords = None
        while True:
            pos = get_mouse_position()
            if pos:
                name, x, y, px, py = pos
                if "liftoff" in name.lower():
                    current = (x, y)
                    if current != last_coords:
                        print(f"Target: Liftoff | Pixel: X={x:<4} Y={y:<4} | Percentage: X={px:.4f} Y={py:.4f}")
                        last_coords = current
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nHelper stopped.")

if __name__ == "__main__":
    main()
