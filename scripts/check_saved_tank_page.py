#!/usr/bin/env python3
"""Test the coordinator's parsing logic with the actual HTML."""
import re
from bs4 import BeautifulSoup

# Read the saved HTML
with open('tank_page.html', 'r') as f:
    html_content = f.read()

soup = BeautifulSoup(html_content, 'html.parser')
data = {}

print("Testing new coordinator parsing logic...")
print("=" * 60)

# Get tank size
tank_size_input = soup.find('input', {'id': 'tank_size'})
if tank_size_input and tank_size_input.get('value'):
    data["capacity_litres"] = int(tank_size_input['value'])
    print(f"✓ Found tank capacity (new format): {data['capacity_litres']} litres")
else:
    # Try old ID format as fallback
    tank_size_input = soup.find('input', {'id': 'tank-size-count'})
    if tank_size_input and tank_size_input.get('value'):
        data["capacity_litres"] = int(tank_size_input['value'])
        print(f"✓ Found tank capacity (old format): {data['capacity_litres']} litres")
    else:
        print("✗ Tank capacity: NOT FOUND")

# Get tank height
tank_height_input = soup.find('input', {'id': 'internal_height'})
if tank_height_input and tank_height_input.get('value'):
    data["height_cm"] = int(tank_height_input['value'])
    print(f"✓ Found tank height (new format): {data['height_cm']} cm")
else:
    # Try old ID format as fallback
    tank_height_input = soup.find('input', {'id': 'tank-height-count'})
    if tank_height_input and tank_height_input.get('value'):
        data["height_cm"] = int(tank_height_input['value'])
        print(f"✓ Found tank height (old format): {data['height_cm']} cm")
    else:
        print("✗ Tank height: NOT FOUND")

print("=" * 60)

# Get tank level percentage (OLD method - should not find it)
total_level_input = soup.find('input', {'name': 'percentage'})
if total_level_input and total_level_input.get('value'):
    data["total_level_percentage"] = int(total_level_input['value'])
    print(f"✓ Found total tank level (OLD): {data['total_level_percentage']}%")
else:
    print("✗ Total level input (name='percentage'): NOT FOUND (expected)")

# Get usable oil level percentage (or total oil level in new interface)
usable_level_div = soup.find("div", {"id": "usable-oil"})
if usable_level_div:
    oil_level = usable_level_div.find("div", {"class": "oil-level"})
    if oil_level and oil_level.get("data-percentage"):
        level_percentage = float(oil_level["data-percentage"])
        data["usable_level_percentage"] = level_percentage
        print(f"✓ Found oil level: {level_percentage}%")
        
        # If we didn't find the old total_level field, use this as total too
        if "total_level_percentage" not in data:
            data["total_level_percentage"] = level_percentage
            print(f"✓ Using oil level as total level (new interface): {level_percentage}%")

# Look for volume information in text
volume_texts = soup.find_all(string=lambda text: text and any(word in text.lower() for word in ['litre', 'volume', 'oil level']))
for text in volume_texts:
    text = text.strip()

    # Extract usable volume
    if "usable oil" in text.lower():
        match = re.search(r'(\d+)\s*litres?\s+of\s+usable\s+oil', text.lower())
        if match:
            data["usable_volume_litres"] = int(match.group(1))
            print(f"✓ Found usable volume: {data['usable_volume_litres']} litres")

    # Extract total volume - try multiple patterns
    if "litres of oil left" in text.lower() or "litres of oil" in text.lower():
        match = re.search(r'(\d+)\s*litres?\s+of\s+(?:total\s+)?oil', text.lower())
        if not match:
            match = re.search(r'(\d+)l\s+of\s+(?:total\s+)?oil', text.lower())
        if match:
            data["current_volume_litres"] = int(match.group(1))
            print(f"✓ Found current volume: {data['current_volume_litres']} litres")
            break

# If we found current_volume but not usable_volume, they're now the same
if "current_volume_litres" in data and "usable_volume_litres" not in data:
    data["usable_volume_litres"] = data["current_volume_litres"]
    print(f"✓ Using current volume as usable volume: {data['usable_volume_litres']} litres")

print("\n" + "=" * 60)
print("FINAL DATA EXTRACTED:")
print("=" * 60)
for key, value in sorted(data.items()):
    print(f"{key:30s}: {value}")

print("\n" + "=" * 60)
print("VALIDATION:")
print("=" * 60)

# Check that we have all required fields
required_fields = ["total_level_percentage", "usable_level_percentage", "current_volume_litres", "usable_volume_litres", "capacity_litres", "height_cm"]
all_found = True
for field in required_fields:
    if field in data:
        print(f"✓ {field}: PRESENT")
    else:
        print(f"✗ {field}: MISSING")
        all_found = False

if all_found:
    print("\n✓ SUCCESS: All required fields extracted!")
else:
    print("\n✗ FAILURE: Some fields missing")

# Check if total and usable are the same (expected in new interface)
if data.get("total_level_percentage") == data.get("usable_level_percentage"):
    print("✓ Total and usable levels are the same (new interface confirmed)")
else:
    print("⚠ Total and usable levels differ (old interface or mixed data)")

if data.get("current_volume_litres") == data.get("usable_volume_litres"):
    print("✓ Current and usable volumes are the same (new interface confirmed)")
else:
    print("⚠ Current and usable volumes differ (old interface or mixed data)")

