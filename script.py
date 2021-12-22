import json
import logging
import requests
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import Select
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from dataclasses import dataclass
from locale import atof, setlocale, LC_NUMERIC
from typing import IO, Tuple
import subprocess
import traceback

TOR_PATH = "/usr/local/bin/tor"
TOR_PORT = 9050
GECKO_DRIVER_PATH = './geckodriver'

Percentage = float
DollarAmount = float
Year = int

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s:%(funcName)s:%(message)s")


@dataclass
class Unit:
  beds: float
  baths: float


class Listing:
  '''
  Listing is a single listing.
  '''
  def __init__(self, raw_listing: dict):
    self.raw_listing = raw_listing

  @property
  def price(self) -> DollarAmount:
    return DollarAmount(self.raw_listing['price']['listed'])

  @property
  def pretty_address(self) -> str:
    '''
    returns an address in the form
    "276 Lakefield Pl, Moraga, CA 94556"
    '''
    location = self.raw_listing['location']
    street_addr = location['prettyAddress']
    city = location['city']
    state = location['state']
    zip_code = location['zipCode']
    return street_addr + ', ' + city + ', ' + state + ' ' + zip_code

  @property
  def units(self) -> list[Unit]:
    '''
    [{'name': 'Unit 1',
    'fields': [{'key': 'Unit 1 Baths', 'values': ['1']},
      {'key': 'Unit 1 Bedrooms', 'values': ['3']},
      {'key': 'Unit 1 Rental Amt Freq', 'values': ['Monthly']},
      {'key': 'Unit 1 Status', 'values': ['Month to Month']},
      {'key': 'Unit 1 Rental Amount', 'values': ['$1,200.00']}]},
    {'name': 'Unit 2',
    'fields': [{'key': 'Unit 2 Lease Term', 'values': ['Month to Month']},
      {'key': 'Unit 2 Baths', 'values': ['1']},
      {'key': 'Unit 2 Rental Amt Freq', 'values': ['Monthly']},
      {'key': 'Unit 2 Bedrooms', 'values': ['2']},
      {'key': 'Unit 2 Rental Amount', 'values': ['$1,150.00']}]}]

    TODO: multi family info may be laid out differently i.e.: https://www.compass.com/listing/1050-palafox-drive-northeast-atlanta-ga-30324/845726107540165857/
    '''
    logging.debug(f"Attempting to extract unit information")
    raw_units = []

    for detail in self.raw_listing['detailedInfo']['listingDetails']:
      logging.debug(f"Examining detail: {detail}")
      if detail.get('name') == 'Unit Information':
        raw_units = detail['subCategories']
        logging.debug(
            f"Found the 'Unit Information' detail, attempting to extract units from raw_units: {raw_units}"
        )

    units: list[Unit] = []
    for raw_unit in raw_units:
      beds = 0.0
      baths = 0.0
      for field in raw_unit['fields']:
        if 'Baths' in field['key']:
          pass
          if 'Half' in field['key']:
            logging.debug(f"Interpreting {field['key']} as half bath")
            baths += 0.5 * float(field['values'][0])
          else:
            logging.debug(f"Interpreting {field['key']} as full bath")
            baths += int(field['values'][0])
        elif 'Bedrooms' in field['key']:
          logging.debug(f"Interpreting {field['key']} as bedroom")
          beds = int(field['values'][0])
      units.append(Unit(beds, baths))

    return units


def from_raw(raw: str) -> Listing:
  '''
  from_raw takes in the raw webpage returned by an indivudal compass multi-family
  listing and extracts the raw listing information from the javascript.
  '''
  logging.info("Extracting raw listing")
  return Listing(
      json.loads(
          raw.split("window.__PARTIAL_INITIAL_DATA__ = ")[1].split("</script>")
          [0].strip())['props']['listingRelation']['listing'])


@dataclass
class RentEstimate:
  unit: Unit
  average: DollarAmount
  median: DollarAmount
  twenty_fifth_percentile: DollarAmount
  seventy_fifth_percentile: DollarAmount


class RentEstimator(object):
  '''
  RentEstimator uses selenium (ff proxied through TOR) to query https://www.rentometer.com/ for a rent estimate.
  geckodriver is the path to the Gecko driver binary (https://github.com/mozilla/geckodriver/releases).
  '''
  def __init__(
      self,
      geckodriver: str = GECKO_DRIVER_PATH,
  ):
    self.browser: webdriver.Firefox
    self.tor: subprocess.Popen[bytes]
    self.geckodriver = geckodriver
    self.estimates: list[RentEstimate]

  # TODO: could become a RentometerBrowser class
  def _get_unthrottled_tor_browser(
      self) -> Tuple[subprocess.Popen[bytes], webdriver.Firefox]:
    '''
    Sometimes if the TOR output node is known to Rentometer (or perhaps by some other mechanism),
    Rentometer will say that your free search limit is reached and the "Analyze" button will be inactive.
    Keep restarting TOR and loading up Rentometer until we get a page where we can actually hit "Analyze".
    '''
    # Set up TOR proxy options.
    options = Options()
    options.headless = False  #TODO
    options.set_preference('network.proxy.type', 1)
    options.set_preference('network.proxy.socks', '127.0.0.1')
    options.set_preference('network.proxy.socks_port', TOR_PORT)

    # Set Selenium to become active as soon as the page becomes interactive,
    # rather than waiting until it's fully loaded.
    capabilities = DesiredCapabilities().FIREFOX
    capabilities["pageLoadStrategy"] = "eager"

    analyze_button_disabled = True
    while analyze_button_disabled:
      logging.info("Starting TOR...")
      # Start TOR and wait for it to boot up.
      # TODO: add a timeout here.
      tor = subprocess.Popen(TOR_PATH, stdout=subprocess.PIPE)
      maybe_stdout = tor.stdout
      if maybe_stdout is None:
        raise Exception("stdout was None")
      stdout: IO[bytes] = maybe_stdout
      while True:
        line = stdout.readline()
        logging.debug(f"TOR startup output: {'{!r}'.format(line)}")
        if b"100% (done): Done" in line:
          break
      logging.info(f"TOR started successfully")

      # Create a TOR browser
      logging.info(f"Opening browser...")
      browser = webdriver.Firefox(service=Service(self.geckodriver),
                                  options=options,
                                  capabilities=capabilities)

      # Connect to Rentometer and check if the analyze_button is disabled.
      logging.info(f"Connecting to https://www.rentometer.com/")
      browser.get("https://www.rentometer.com/")
      logging.info(f"https://www.rentometer.com/ connection succeeded")
      logging.info(f"Checking if the \"Analyze\" button is enabled")
      analyze_button = browser.find_element_by_name("commit")
      analyze_button_disabled = analyze_button.get_attribute(
          "disabled") == "true"

      # If the analyze button is disabled, kill the browser and TOR and try again.
      if analyze_button_disabled:
        logging.warning(
            "Opened Rentometer \"Analyze\" button was disabled. Killing the browser and TOR and trying again."
        )
        browser.close()
        tor.kill()
      else:
        logging.info(
            "Got a Rentometer browser with the \"Analyze\" button enabled.")

    return (tor, browser)

  def _nuke_tor_browser(self):
    # Close the browser and kill TOR.
    logging.info("Closing the browser and killing TOR")
    try:
      self.browser.close()
    except AttributeError:
      # self.browser wasn't set
      pass
    try:
      self.tor.kill()
    except AttributeError:
      # self.tor wasn't set
      pass

  def estimate(self, listing: Listing) -> list[RentEstimate]:
    logging.info(f"Estimating the rents at {listing.pretty_address}")

    self.estimates = []
    self.tor, self.browser = self._get_unthrottled_tor_browser()

    try:
      for unit in listing.units:
        logging.info(f"Estimating rent for unit: {unit}")

        # Find the relevant UI elements
        analyze_button = self.browser.find_element_by_name("commit")
        if analyze_button.get_attribute("disabled") == "true":
          # If rentometer is throttling our Analyze requests, kill the browser and TOR and reboot them until we can analyze again.
          self._nuke_tor_browser()
          self.tor, self.browser = self._get_unthrottled_tor_browser()

        address_box = self.browser.find_element_by_id(
            "address_unified_search_address")
        beds_selector = Select(
            self.browser.find_element_by_id(
                "address_unified_search_bed_style"))
        baths_selector = Select(
            self.browser.find_element_by_id("address_unified_search_baths"))

        address_box.send_keys(listing.pretty_address)
        logging.info(f"Entered {listing.pretty_address} into the address box")

        beds_selector.select_by_value(str(
            unit.beds))  # "1" = 1 bed, "2" = 2 beds, etc.
        logging.info(f"Selected {unit.beds} for \"Beds\"")

        # <option value="">Any</option>
        # <option value="1">1 Only</option>
        # <option value="1.5">1½ or more</option>
        if unit.baths == 1:
          baths_selector.select_by_value("1")
          logging.info(f"Selected \"1 Only\" for \"Baths\"")
        elif unit.baths > 1:
          baths_selector.select_by_value("1.5")
          logging.info(f"Selected \"1½ or more\" for \"Baths\"")
        else:
          raise ValueError(f"Unexpected number of baths: {unit.baths}")

        # Click analyze button to get analysis (typically opens a new page).
        logging.info("Clicking the analyze button")
        analyze_button.click()

        # Check that rentometer was able to find enough results.
        try:
          if "Sorry, there are not enough results in that location to generate a valid analysis." in self.browser.find_element_by_xpath(
              "/html/body/div[3]/div").text:
            logging.error(
                "\"Sorry, there are not enough results in that location to generate a valid analysis.\""
            )
            # TODO all the above logic should be wrapped in a try logic, if the exception that will be thrown here is caught then
            # we should retry with "Any" baths.
            continue
        except NoSuchElementException:
          # This is the happy path.
          logging.info("Analysis succeeded")

        # Now we're on the analysis page.
        stats: list[WebElement] = self.browser.find_elements_by_class_name(
            "box-stats")
        average: DollarAmount = 0
        median: DollarAmount = 0
        twenty_fifth_percentile: DollarAmount = 0
        seventy_fifth_percentile: DollarAmount = 0
        setlocale(LC_NUMERIC, '')  # set to default locale

        def extract_dollar_value(stat: WebElement) -> DollarAmount:
          return DollarAmount(atof(stat.text.split('$')[-1]))

        for stat in stats:
          if 'AVERAGE' in stat.text:
            average = extract_dollar_value(stat)
          elif 'MEDIAN' in stat.text:
            median = extract_dollar_value(stat)
          elif '25TH PERCENTILE' in stat.text:
            twenty_fifth_percentile = extract_dollar_value(stat)
          elif '75TH PERCENTILE' in stat.text:
            seventy_fifth_percentile = extract_dollar_value(stat)
          else:
            logging.warning(f"Unexpected stat in stats box: {stat.text}")

        if average == 0 or median == 0 or twenty_fifth_percentile == 0 or seventy_fifth_percentile == 0:
          logging.error(
              f"Could not extract stat from stats box: {[s.text for s in stats]}"
          )

        estimate = RentEstimate(unit, average, median, twenty_fifth_percentile,
                                seventy_fifth_percentile)

        logging.info(
            f"Successfully created rent estimate for {listing.pretty_address}: {estimate}"
        )

        self.estimates.append(estimate)
    except Exception:
      logging.error(
          f"Unexpected error encountered while creating rent estimate: {traceback.format_exc()}"
      )
    finally:
      self._nuke_tor_browser()

    return self.estimates


# TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO TODO
class SpreadSheetBuilder(object):
  pass


if __name__ == '__main__':
  page = requests.get(
      "https://www.compass.com/listing/689-auburn-street-manchester-nh-03103/932977102909516985/"
  )

  listing = from_raw(page.text)
  re = RentEstimator()
  estimates = re.estimate(listing)
  for estimate in estimates:
    print(estimate)

  exit(0)
