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
        match = re.search(r"Position: (\d+),(\d+)", geom)
        if match:
            rel_x = int(x) - int(match.group(1))
            rel_y = int(y) - int(match.group(2))
            return name, rel_x, rel_y
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
    print("1. Open Liftoff in windowed mode (e.g. 1280x720).")
    print("2. Hover your mouse over the menus (Multiplayer, Create Room, etc.).")
    print("3. Look at this terminal to get the exact X and Y coordinates relative to the Liftoff window.")
    print("Press Ctrl+C to stop.\n")
    
    try:
        last_coords = None
        while True:
            pos = get_mouse_position()
            if pos:
                name, x, y = pos
                if "liftoff" in name.lower():
                    current = (x, y)
                    if current != last_coords:
                        print(f"Target: Liftoff | X: {x:<5} Y: {y:<5} (Hovering over button)")
                        last_coords = current
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nHelper stopped.")

if __name__ == "__main__":
    main()
