import os
import sys
import json
import time
import subprocess
import re
import argparse

def load_config():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lobby_config.json")
    if not os.path.exists(config_path):
        print(f"ERROR: Configuration file not found at {config_path}")
        sys.exit(1)
    with open(config_path, "r") as f:
        return json.load(f)

def run_command(cmd, env=None, check=True):
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, check=check)
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {' '.join(cmd)}\nError: {e.stderr}")
        raise

def take_screenshot(display, filename="bot_view.png"):
    """
    Takes a screenshot of the virtual X display using scrot or import (ImageMagick).
    """
    env = os.environ.copy()
    env["DISPLAY"] = display
    
    # Try scrot first
    try:
        subprocess.run(["scrot", "-z", filename], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[Bot] Screenshot saved to: {filename}")
        return True
    except FileNotFoundError:
        pass
        
    # Try ImageMagick import
    try:
        subprocess.run(["import", "-window", "root", filename], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[Bot] Screenshot saved to: {filename}")
        return True
    except FileNotFoundError:
        pass
        
    print("[Bot] WARNING: Neither 'scrot' nor 'import' (ImageMagick) is installed. Cannot take screenshots.")
    return False

def get_window_id(display):
    """
    Finds the window ID for Liftoff in the given display.
    """
    env = os.environ.copy()
    env["DISPLAY"] = display
    try:
        out = run_command(["xdotool", "search", "--onlyvisible", "--class", "Liftoff"], env=env, check=False)
        if out:
            # Return the first window ID found
            return out.splitlines()[0]
    except Exception:
        pass
    return None

def get_window_size(display, win_id):
    env = os.environ.copy()
    env["DISPLAY"] = display
    try:
        geom = run_command(["xdotool", "getwindowgeometry", win_id], env=env)
        match = re.search(r"Geometry: (\d+)x(\d+)", geom)
        if match:
            return int(match.group(1)), int(match.group(2))
    except Exception:
        pass
    return 1280, 720  # Fallback

def click_at(display, win_id, x, y, delay=0.5):
    """
    Clicks at a coordinate. Automatically supports float percentages (0.0 to 1.0)
    or raw integer pixels.
    """
    env = os.environ.copy()
    env["DISPLAY"] = display
    
    # Check if inputs are float percentages
    if (isinstance(x, float) and 0.0 <= x <= 1.0) or (isinstance(y, float) and 0.0 <= y <= 1.0):
        width, height = get_window_size(display, win_id)
        x_pixel = int(x * width)
        y_pixel = int(y * height)
        print(f"[Bot] Scaling percentage ({x:.4f}, {y:.4f}) to pixels: X: {x_pixel}, Y: {y_pixel} (based on size {width}x{height})")
        x, y = x_pixel, y_pixel
        
    print(f"[Bot] Clicking at X: {x}, Y: {y} relative to window {win_id}...")
    run_command(["xdotool", "mousemove", "--window", win_id, str(x), str(y)], env=env)
    time.sleep(delay)
    run_command(["xdotool", "click", "--window", win_id, "1"], env=env)
    time.sleep(0.5)

def press_key(display, win_id, key, delay=0.5):
    env = os.environ.copy()
    env["DISPLAY"] = display
    print(f"[Bot] Pressing key: '{key}' on window {win_id}...")
    run_command(["xdotool", "key", "--window", win_id, key], env=env)
    time.sleep(delay)

def type_text(display, win_id, text, delay=0.5):
    env = os.environ.copy()
    env["DISPLAY"] = display
    print(f"[Bot] Typing text: '{text}' into window {win_id}...")
    run_command(["xdotool", "type", "--window", win_id, text], env=env)
    time.sleep(delay)

def main():
    parser = argparse.ArgumentParser(description="Liftoff Headless Multiplayer Lobby Automation Bot")
    parser.add_argument("--test-clicks", action="store_true", help="Test coordinates by performing a single loop of clicks with screenshots.")
    parser.add_argument("--interval", type=int, default=600, help="Rotation interval in seconds (default: 600s / 10m).")
    args = parser.parse_args()

    config = load_config()
    display = config.get("display", ":99")
    liftoff_path = config.get("liftoff_path")
    coords = config["coordinates"]
    
    # 1. Start Xvfb if not running
    print("[Bot] Checking virtual framebuffer (Xvfb)...")
    xvfb_running = False
    try:
        out = run_command(["pgrep", "Xvfb"], check=False)
        if out:
            xvfb_running = True
            print("[Bot] Xvfb is already running.")
    except Exception:
        pass
        
    if not xvfb_running:
        print(f"[Bot] Starting Xvfb on display {display}...")
        subprocess.Popen(["Xvfb", display, "-screen", "0", "1280x720x24"])
        time.sleep(2)

    # 2. Launch Liftoff if not running
    print("[Bot] Checking if Liftoff is running...")
    game_running = False
    try:
        out = run_command(["pgrep", "Liftoff"], check=False)
        if out:
            game_running = True
            print("[Bot] Liftoff is already running.")
    except Exception:
        pass

    if not game_running:
        if not os.path.exists(liftoff_path):
            print(f"ERROR: Liftoff executable not found at: {liftoff_path}")
            sys.exit(1)
        print("[Bot] Starting Liftoff...")
        env = os.environ.copy()
        env["DISPLAY"] = display
        subprocess.Popen([
            liftoff_path, 
            "-screen-width", "1280", 
            "-screen-height", "720", 
            "-screen-fullscreen", "0"
        ], env=env)
        print("[Bot] Waiting for Liftoff window to load...")
        
    win_id = None
    for _ in range(30): # Wait up to 30 seconds
        win_id = get_window_id(display)
        if win_id:
            break
        time.sleep(1)
        
    if not win_id:
        print("ERROR: Failed to find Liftoff window. Check if the game started successfully.")
        take_screenshot(display, "error_startup.png")
        sys.exit(1)
        
    print(f"[Bot] Found Liftoff window. ID: {win_id}")
    
    # Position and focus window
    env = os.environ.copy()
    env["DISPLAY"] = display
    run_command(["xdotool", "windowmove", win_id, "0", "0"], env=env)
    run_command(["xdotool", "windowsize", win_id, "1280", "720"], env=env)
    run_command(["xdotool", "windowactivate", win_id], env=env)
    time.sleep(1)

    # Take initial screenshot
    take_screenshot(display, "screenshot_start.png")

    if args.test_clicks:
        print("\n" + "="*80)
        print("TEST CLICKS MODE")
        print("="*80)
        print("This will execute the menu navigation clicks slowly and take screenshots to verify coordinates.")
        
        # 1. Click Multiplayer
        click_at(display, win_id, *coords["multiplayer_btn"])
        take_screenshot(display, "test_1_multiplayer.png")
        
        # 2. Click Create Room
        click_at(display, win_id, *coords["create_room_btn"])
        take_screenshot(display, "test_2_create_room.png")
        
        # 3. Click Room Name Input and type name
        click_at(display, win_id, *coords["room_name_input"])
        type_text(display, win_id, config["lobby_name"])
        take_screenshot(display, "test_3_typed_name.png")
        
        # 4. Click Select Track
        click_at(display, win_id, *coords["select_track_btn"])
        take_screenshot(display, "test_4_select_track.png")
        
        # 5. Select first track
        click_at(display, win_id, *coords["first_track_in_list"])
        take_screenshot(display, "test_5_clicked_track.png")
        
        # 6. Apply Track selection
        click_at(display, win_id, *coords["track_select_apply_btn"])
        take_screenshot(display, "test_6_applied_track.png")
        
        # 7. Start Lobby
        click_at(display, win_id, *coords["lobby_start_btn"])
        take_screenshot(display, "test_7_started_lobby.png")
        
        print("\nTest completed successfully. Check the test_*.png screenshots in your folder!")
        sys.exit(0)

    # Standard loop mode
    print("\n[Bot] Bot is ready. Starting rotation cycle...")
    try:
        # Start initial lobby
        print("[Bot] Navigating to create multiplayer room...")
        click_at(display, win_id, *coords["multiplayer_btn"])
        click_at(display, win_id, *coords["create_room_btn"])
        click_at(display, win_id, *coords["room_name_input"])
        type_text(display, win_id, config["lobby_name"])
        click_at(display, win_id, *coords["select_track_btn"])
        click_at(display, win_id, *coords["first_track_in_list"])
        click_at(display, win_id, *coords["track_select_apply_btn"])
        click_at(display, win_id, *coords["lobby_start_btn"])
        
        track_index = 1
        while True:
            print(f"[Bot] Lobby running track {track_index}. Waiting for {args.interval}s before next rotation...")
            time.sleep(args.interval)
            
            track_index += 1
            print(f"\n[Bot] Rotating map to track index: {track_index}...")
            
            # Click Pause/Menu Button (or press Esc)
            press_key(display, win_id, "Escape")
            # Click End Game / Stop Room
            click_at(display, win_id, *coords["end_game_btn"])
            # Click Change Settings / Change Track
            click_at(display, win_id, *coords["change_track_btn"])
            # Click Select Track
            click_at(display, win_id, *coords["select_track_btn"])
            
            # Navigate track list:
            # To click the next track in the list:
            # We can either use arrow keys (Down arrow) to select the next track in the list and click Enter,
            # or simulate scroll. Emulating Down arrow key is much more robust!
            press_key(display, win_id, "Down")
            
            # Click Apply Track
            click_at(display, win_id, *coords["track_select_apply_btn"])
            # Click Start Lobby
            click_at(display, win_id, *coords["lobby_start_btn"])
            
    except KeyboardInterrupt:
        print("\n[Bot] Stopped by user request. Exiting...")
        sys.exit(0)

if __name__ == "__main__":
    main()
