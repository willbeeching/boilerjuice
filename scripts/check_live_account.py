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

def test_connection(session: requests.Session, email: str, password: str) -> bool:
    """Test if we can connect to BoilerJuice and login successfully."""
    try:
        # First, get the login page to get the CSRF token
        print("\nTesting connection...")
        print(f"1. Accessing login page at {LOGIN_URL}")
        response = session.get(LOGIN_URL)

        if response.status_code != 200:
            print(f"Error: Failed to access login page with status code {response.status_code}")
            print(f"Response URL: {response.url}")
            print("Response headers:", dict(response.headers))
            return False

        print("   ✓ Successfully accessed login page")
        print("   Response length:", len(response.text))

        # Try to find the CSRF token
        soup = BeautifulSoup(response.text, 'html.parser')
        csrf_token = soup.find('meta', {'name': 'csrf-token'})

        if not csrf_token:
            print("Error: Could not find CSRF token")
            print("\nAvailable meta tags:")
            for meta in soup.find_all('meta'):
                print(f"- {meta.get('name', 'no name')}: {meta.get('content', 'no content')}")
            return False

        csrf_token = csrf_token['content']
        print("   ✓ Found CSRF token")

        # Attempt login
        print("\n2. Testing login...")
        login_data = {
            'user[email]': email,
            'user[password]': password,
            'authenticity_token': csrf_token,
            'commit': 'Sign in'
        }

        response = session.post(LOGIN_URL, data=login_data)

        if response.status_code != 200:
            print(f"Error: Login failed with status code {response.status_code}")
            print(f"Response URL: {response.url}")
            print("Response headers:", dict(response.headers))
            return False

        # Check if we're still on the login page (indicating failed login)
        if "Sign in" in response.text:
            print("Error: Login failed - still on login page")
            print("Response length:", len(response.text))
            print("\nPage content preview:")
            print(response.text[:500])
            return False

        print("   ✓ Successfully logged in")

        # Verify we can access the account page
        print("\n3. Testing account access...")
        response = session.get(ACCOUNT_URL)

        if response.status_code != 200:
            print(f"Error: Failed to access account page with status code {response.status_code}")
            return False

        if "Sign in" in response.text:
            print("Error: Not properly logged in - redirected to login page")
            return False

        print("   ✓ Successfully accessed account page")
        print("\nConnection test successful! ✓")
        return True

    except Exception as e:
        print(f"\nError during connection test: {str(e)}")
        return False

def get_tank_id(session: requests.Session) -> str | None:
    """Get the tank ID from the tanks page."""
    print("\nAccessing tanks page to find tank ID...")
    tanks_url = f"{BASE_URL}/users/tanks"
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
        print("\nAvailable links:")
        for link in soup.find_all('a'):
            print(f"- {link.get('href')}")
        return None

    # Get the first tank ID
    tank_id = re.search(r'/uk/users/tanks/(\d+)', tank_links[0]['href']).group(1)
    print(f"Found tank ID: {tank_id}")
    return tank_id

def extract_js_variable(script_text: str, var_name: str) -> str | None:
    """Extract a JavaScript variable value."""
    try:
        start = script_text.find(f'var {var_name} = ') + len(f'var {var_name} = ')
        if start > -1:
            # Find the end of the assignment (either semicolon or newline)
            end = script_text.find(';', start)
            if end == -1:
                end = script_text.find('\n', start)
            if end > -1:
                return script_text[start:end].strip()
    except Exception as e:
        print(f"Error extracting {var_name}: {e}")
    return None

def get_tank_details(session: requests.Session, tank_id: str) -> dict | None:
    """Get the tank details from BoilerJuice."""
    tank_url = f"{BASE_URL}/users/tanks/{tank_id}/edit"
    print(f"\nAccessing tank page at {tank_url}")

    response = session.get(tank_url)
    if response.status_code != 200:
        print(f"Error: Failed to get tank page with status code {response.status_code}")
        return None

    soup = BeautifulSoup(response.text, 'html.parser')
    tank_details = {}

    # First examine JavaScript data
    print("\nExamining JavaScript data...")
    scripts = soup.find_all('script')
    for script in scripts:
        if not script.string:
            continue

        script_text = script.string

        # Look for various tank-related variables
        for var_name in ['tank', 'volume', 'tankData', 'tankVolume', 'tankDetails', 'oilLevel']:
            data = extract_js_variable(script_text, var_name)
            if data:
                print(f"\nFound {var_name} in JavaScript:")
                print(data)
                try:
                    json_data = json.loads(data)
                    print(f"\nParsed {var_name} data:")
                    print(json.dumps(json_data, indent=2))
                except json.JSONDecodeError:
                    print(f"Could not parse {var_name} as JSON")

    # Now get the visible data
    print("\nExtracting visible data...")

    # Get total oil level percentage
    total_level_input = soup.find("input", {"name": "percentage"})
    if total_level_input and total_level_input.get("value"):
        tank_details["total_level_percentage"] = int(total_level_input['value'])
        print(f"Total Level: {tank_details['total_level_percentage']}%")

    # Get usable oil level percentage
    usable_level_div = soup.find("div", {"id": "usable-oil"})
    if usable_level_div:
        oil_level = usable_level_div.find("div", {"class": "oil-level"})
        if oil_level and oil_level.get("data-percentage"):
            tank_details["usable_level_percentage"] = float(oil_level["data-percentage"])
            print(f"Usable Level: {tank_details['usable_level_percentage']}%")

    # Get tank size
    tank_size_input = soup.find('input', {'id': 'tank-size-count'})
    if tank_size_input and tank_size_input.get('value'):
        tank_details["capacity_litres"] = int(tank_size_input['value'])
        print(f"Capacity: {tank_details['capacity_litres']} litres")

    # Look for volume information in text
    print("\nSearching for volume information in text...")
    volume_texts = soup.find_all(string=lambda text: text and any(word in text.lower() for word in ['litre', 'volume', 'oil level']))
    for text in volume_texts:
        text = text.strip()
        print(f"Found: {text}")

        # Extract usable volume
        if "usable oil" in text.lower():
            match = re.search(r'(\d+)\s*litres?\s+of\s+usable\s+oil', text.lower())
            if match:
                tank_details["usable_volume_litres"] = int(match.group(1))
                print(f"Usable Volume: {tank_details['usable_volume_litres']} litres")

        # Extract total volume
        elif "litres of oil left" in text.lower():
            match = re.search(r'(\d+)\s*litres?\s+of\s+oil\s+left', text.lower())
            if match:
                tank_details["current_volume_litres"] = int(match.group(1))
                print(f"Total Volume: {tank_details['current_volume_litres']} litres")

    # Look for price information
    print("\nSearching for price information...")
    price_texts = soup.find_all(string=lambda text: text and any(word in text.lower() for word in ['pence', 'p/litre', '£', 'cost', 'price']))
    for text in price_texts:
        text = text.strip()
        print(f"Found price text: {text}")

        # Try to find price per litre in pence
        pence_match = re.search(r'(\d+\.?\d*)\s*(?:p|pence)/litre', text.lower())
        if pence_match:
            tank_details["price_per_litre_pence"] = float(pence_match.group(1))
            print(f"Price per litre: {tank_details['price_per_litre_pence']}p")
            continue

        # Try to find price per litre in pounds
        pounds_match = re.search(r'£(\d+\.?\d*)/litre', text.lower())
        if pounds_match:
            # Convert pounds to pence
            tank_details["price_per_litre_pence"] = float(pounds_match.group(1)) * 100
            print(f"Price per litre: {tank_details['price_per_litre_pence']}p")

    # Get tank model info
    tank_model_input = soup.find('input', {'id': 'tankModelInput'})
    if tank_model_input and tank_model_input.get('value'):
        model_id = tank_model_input.get('value')
        tank_details["model_id"] = model_id
        print(f"\nFound tank model ID: {model_id}")

        # Look for model data in JavaScript
        for script in scripts:
            if script.string and 'var jsonData = ' in script.string:
                start_idx = script.string.find('var jsonData = ')
                if start_idx >= 0:
                    array_start = script.string.find('[', start_idx)
                    if array_start >= 0:
                        bracket_count = 1
                        array_end = array_start + 1
                        while array_end < len(script.string) and bracket_count > 0:
                            if script.string[array_end] == '[':
                                bracket_count += 1
                            elif script.string[array_end] == ']':
                                bracket_count -= 1
                            array_end += 1

                        if bracket_count == 0:
                            try:
                                json_data = json.loads(script.string[array_start:array_end])
                                for item in json_data:
                                    if str(item.get('id')) == str(model_id):
                                        print("\nFound tank data in JSON:")
                                        print(json.dumps(item, indent=2))
                                        tank_details["model"] = item.get('tank', {}).get('Description')
                                        tank_details["manufacturer"] = item.get('tank', {}).get('Brand')
                                        # Store the full tank data for examination
                                        tank_details["tank_data"] = item.get('tank', {})
                                        break
                            except json.JSONDecodeError as e:
                                print(f"Failed to parse JSON: {e}")

    return tank_details

def get_oil_price(session):
    """Get the current oil price from the kerosene prices page."""
    try:
        # Navigate to the kerosene prices page
        response = session.get("https://www.boilerjuice.com/kerosene-prices/")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        # Look for the current price in the page content
        price_elements = soup.find_all(['div', 'p', 'span'])
        for element in price_elements:
            text = element.get_text().strip()
            if not text:
                continue

            # Look for price patterns
            price_match = None
            # Pattern: XX.XX pence per litre
            if 'pence per litre' in text.lower():
                match = re.search(r'(\d+\.?\d*)\s*pence per litre', text.lower())
                if match:
                    price_match = float(match.group(1))
            # Pattern: XX.XXp/litre
            elif 'p/litre' in text.lower():
                match = re.search(r'(\d+\.?\d*)p/litre', text.lower())
                if match:
                    price_match = float(match.group(1))
            # Pattern: £X.XX/litre
            elif '£' in text and '/litre' in text.lower():
                match = re.search(r'£(\d+\.?\d*)/litre', text.lower())
                if match:
                    price_match = float(match.group(1)) * 100  # Convert pounds to pence

            if price_match is not None:
                return {
                    'price_pence': price_match,
                    'source_text': text
                }

        print("Could not find current price on the kerosene prices page")
        return None

    except Exception as e:
        print(f"Error getting oil price: {str(e)}")
        return None

def main():
    """Main function."""
    email = os.getenv("BOILERJUICE_EMAIL")
    password = os.getenv("BOILERJUICE_PASSWORD")

    if not email or not password:
        print("Error: Please set BOILERJUICE_EMAIL and BOILERJUICE_PASSWORD environment variables")
        sys.exit(1)

    try:
        session = requests.Session()

        # First test the connection
        if not test_connection(session, email, password):
            print("\nConnection test failed. Please check your credentials and try again.")
            sys.exit(1)

        # Get oil price information
        price_info = get_oil_price(session)

        # Get tank ID
        tank_id = os.getenv("BOILERJUICE_TANK_ID")
        if not tank_id:
            tank_id = get_tank_id(session)
            if not tank_id:
                print("Error: Could not find tank ID")
                sys.exit(1)

        # Get tank details
        tank_details = get_tank_details(session, tank_id)
        if tank_details:
            # Add price information if found
            if price_info:
                tank_details.update(price_info)

            print("\nSummary of findings:")
            print("-" * 40)
            for key, value in tank_details.items():
                if key != "tank_data":  # Skip the full tank data in summary
                    print(f"{key}: {value}")

            if "tank_data" in tank_details:
                print("\nFull tank specifications:")
                print("-" * 40)
                print(json.dumps(tank_details["tank_data"], indent=2))

            # After getting tank details, try to get the price
            if price_info:
                print("\nCurrent oil price:")
                print(f"Price: {price_info['price_pence']}p per litre")
                print(f"Source text: {price_info['source_text']}")
        else:
            print("Error: Could not retrieve tank details")
            sys.exit(1)

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()