#!/bin/bash
set -e

TRACK_IDS=(
  3063613366 # Part 1
  3063746252 # Part 2
  3064465286 # Part 3
  3065781873 # Part 4
  3066086444 # Part 5
  3066277835 # Part 6
  3066296523 # Part 7
  3067032230 # Part 8
  3067920271 # Part 9
)

SRC_DIR="$HOME/.steam/debian-installation/steamapps/workshop/content/410340"
DST_DIR="/home/fpv_bot/.steam/debian-installation/steamapps/workshop/content/410340"

echo "Copying invntn_ skill series tracks to fpv_bot workshop directory..."
for id in "${TRACK_IDS[@]}"; do
  if [ -d "$SRC_DIR/$id" ]; then
    echo "Copying track folder: $id..."
    sudo cp -r "$SRC_DIR/$id" "$DST_DIR/"
  else
    echo "Warning: Track folder $id not found in $SRC_DIR"
  fi
done

echo "Setting correct owner to fpv_bot..."
sudo chown -R fpv_bot:fpv_bot "$DST_DIR"

echo "Done! Tracks synced successfully."
