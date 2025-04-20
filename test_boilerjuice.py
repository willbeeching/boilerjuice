#!/usr/bin/env python3
"""Test script for BoilerJuice scraping functionality."""
import os
import sys
import re
import time
from dotenv import load_dotenv
import requests
from bs4 import BeautifulSoup
import json

# Load environment variables
load_dotenv()

# Constants
BASE_URL = "https://www.boilerjuice.com/uk"
LOGIN_URL = f"{BASE_URL}/users/login"
ACCOUNT_URL = f"{BASE_URL}/users/account"
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

def get_tank_id(session: requests.Session) -> str:
    """Get the tank ID from the tanks page."""
    print("Accessing tanks page to find tank ID...")
    tanks_url = "https://www.boilerjuice.com/uk/users/tanks"
    response = session.get(tanks_url)

    if response.status_code != 200:
        print(f"Error: Failed to access tanks page with status code {response.status_code}")
        print(f"Response URL: {response.url}")
        return None

    soup = BeautifulSoup(response.text, 'html.parser')

    # Look for links containing 'tank' and extract the ID
    tank_links = soup.find_all('a', href=re.compile(r'/uk/users/tanks/\d+'))

    if not tank_links:
        print("Error: Could not find any tank links on the tanks page")
        print("Available links:")
        for link in soup.find_all('a'):
            print(f"- {link.get('href')}")
        return None

    # Get the first tank ID
    tank_id = re.search(r'/uk/users/tanks/(\d+)', tank_links[0]['href']).group(1)
    print(f"Found tank ID: {tank_id}")
    return tank_id

def get_tank_details(email: str, password: str) -> dict:
    """Get the current tank details from BoilerJuice."""
    session = requests.Session()
    tank_details = {}

    try:
        # First, get the login page to get the CSRF token
        print(f"Accessing login page at {LOGIN_URL}")
        response = session.get(LOGIN_URL)

        if response.status_code != 200:
            print(f"Error: Failed to access login page with status code {response.status_code}")
            print(f"Response URL: {response.url}")
            print("Response content preview:", response.text[:500])
            return None

        soup = BeautifulSoup(response.text, 'html.parser')

        # Find the CSRF token
        csrf_token = soup.find('meta', {'name': 'csrf-token'})
        if not csrf_token:
            print("Error: Could not find CSRF token")
            print("Available meta tags:")
            for meta in soup.find_all('meta'):
                print(f"- {meta.get('name', 'no name')}: {meta.get('content', 'no content')}")
            return None

        csrf_token = csrf_token['content']
        print("Found CSRF token")

        # Login to the site
        login_data = {
            'user[email]': email,
            'user[password]': password,
            'authenticity_token': csrf_token,
            'commit': 'Sign in'
        }

        print("Attempting to log in...")
        response = session.post(LOGIN_URL, data=login_data)

        if response.status_code != 200:
            print(f"Error: Login failed with status code {response.status_code}")
            print(f"Response URL: {response.url}")
            print("Response content preview:", response.text[:500])
            return None

        print("Login successful")

        # Get the tank ID
        tank_id = os.getenv("BOILERJUICE_TANK_ID")
        if not tank_id:
            tank_id = get_tank_id(session)
            if not tank_id:
                return None

        # Get the tank level page with retries
        tank_url = f"https://www.boilerjuice.com/uk/users/tanks/{tank_id}/edit"
        print(f"Accessing tank page at {tank_url}")

        tank_details = {}
        retries = 0

        while retries < MAX_RETRIES:
            response = session.get(tank_url)
            if response.status_code != 200:
                print(f"Error: Failed to get tank page with status code {response.status_code}")
                print(f"Response URL: {response.url}")
                return None

            soup = BeautifulSoup(response.text, 'html.parser')

            # Get tank level percentage
            percentage_input = soup.find("input", {"name": "percentage"})
            if percentage_input and percentage_input.get("value"):
                tank_details["level_percentage"] = int(percentage_input["value"])
                print("Found tank level percentage")

            # Get tank size
            tank_size_input = soup.find("input", {"id": "tank-size-count"})
            if tank_size_input and tank_size_input.get("value"):
                tank_details["capacity_litres"] = int(tank_size_input["value"])
                print("Found tank capacity")

            # Get tank height
            tank_height_input = soup.find("input", {"id": "tank-height-count"})
            if tank_height_input and tank_height_input.get("value"):
                tank_details["height_cm"] = int(tank_height_input["value"])
                print("Found tank height")

            # Get current oil volume estimate
            oil_estimate = soup.find("p", string=lambda text: text and "litres of" in text and "tank" in text)
            if oil_estimate:
                try:
                    volume = int(''.join(filter(str.isdigit, oil_estimate.text)))
                    tank_details["current_volume_litres"] = volume
                    print("Found current oil volume")
                except (ValueError, AttributeError):
                    pass

            # Get tank shape
            tank_shape = None
            for shape in ['cuboid', 'horizontal_cylinder', 'vertical_cylinder']:
                shape_input = soup.find("input", {"type": "radio", "name": "tank-shape", "value": shape})
                if shape_input and shape_input.get("checked"):
                    tank_shape = shape.replace('_', ' ').title()
                    tank_details["shape"] = tank_shape
                    print("Found tank shape")
                    break

            # Get oil type
            oil_type_select = soup.find("select", {"id": "tank_oil_type_id"})
            if oil_type_select:
                selected_option = oil_type_select.find("option", selected=True)
                if selected_option:
                    tank_details["oil_type"] = selected_option.text
                    print("Found oil type")

            # Get tank name
            tank_name_input = soup.find("input", {"id": "tank_user_tanks_attributes_0_name"})
            if tank_name_input and tank_name_input.get("value"):
                tank_details["name"] = tank_name_input["value"]
                print("Found tank name")

            # Get tank manufacturer/model
            tank_model_input = soup.find('input', {'id': 'tankModelInput'})
            if tank_model_input and tank_model_input.get('value'):
                model_id = tank_model_input.get('value')
                tank_details["model_id"] = model_id
                print(f"Found tank model ID: {model_id}")

                # Try to find the manufacturer data in the JavaScript
                scripts = soup.find_all('script')
                for script in scripts:
                    if script.string and 'var jsonData = ' in script.string:
                        print("Found jsonData variable")
                        # Print the context around jsonData
                        script_text = script.string
                        start_idx = script_text.find('var jsonData = ')
                        if start_idx >= 0:
                            print("\nScript context:")
                            context_start = max(0, start_idx - 50)
                            context_end = min(len(script_text), start_idx + 500)
                            print(script_text[context_start:context_end])

                            # Try to find where the JSON array ends
                            array_start = script_text.find('[', start_idx)
                            if array_start >= 0:
                                bracket_count = 1
                                array_end = array_start + 1
                                while array_end < len(script_text) and bracket_count > 0:
                                    if script_text[array_end] == '[':
                                        bracket_count += 1
                                    elif script_text[array_end] == ']':
                                        bracket_count -= 1
                                    array_end += 1

                                if bracket_count == 0:
                                    json_str = script_text[array_start:array_end]
                                    print("\nExtracted JSON array:")
                                    print(json_str[:200])
                                    try:
                                        data = json.loads(json_str)
                                        # Find the manufacturer for our model ID
                                        for item in data:
                                            if str(item.get('id')) == str(model_id):
                                                tank_details["model"] = item.get('tank', {}).get('Description')
                                                print(f"Found manufacturer from JSON: {tank_details['model']}")
                                                break
                                    except json.JSONDecodeError as e:
                                        print(f"Failed to parse JSON: {e}")
                        break
            else:
                print("Could not find tank model ID")

            # Get tank material
            # ... existing code ...

            # Add tank ID to details
            tank_details["id"] = tank_id

            if tank_details:
                return tank_details

            if retries < MAX_RETRIES - 1:
                print(f"Waiting {RETRY_DELAY} seconds before retrying...")
                time.sleep(RETRY_DELAY)
                retries += 1

        if not tank_details:
            print("Error: Could not find any tank details")
            return None

        return tank_details

    except Exception as e:
        print(f"Error: {str(e)}")
        return None

def main():
    """Main function."""
    email = os.getenv("BOILERJUICE_EMAIL")
    password = os.getenv("BOILERJUICE_PASSWORD")

    if not email or not password:
        print("Error: Please set BOILERJUICE_EMAIL and BOILERJUICE_PASSWORD environment variables")
        sys.exit(1)

    try:
        tank_details = get_tank_details(email, password)
        if tank_details is not None:
            print("\nTank Details:")
            if "name" in tank_details:
                print(f"Name: {tank_details['name']}")
            if "shape" in tank_details:
                print(f"Shape: {tank_details['shape']}")
            if "model" in tank_details:
                print(f"Model: {tank_details['model']}")
            print(f"Level: {tank_details.get('level_percentage')}%")
            print(f"Capacity: {tank_details.get('capacity_litres')} litres")
            print(f"Current Volume: {tank_details.get('current_volume_litres')} litres")
            print(f"Tank Height: {tank_details.get('height_cm')} cm")
            if "oil_type" in tank_details:
                print(f"Oil Type: {tank_details['oil_type']}")
            print(f"Tank ID: {tank_details['id']}")
        else:
            print("Error: Could not retrieve tank details")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()