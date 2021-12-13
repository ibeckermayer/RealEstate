import json
import requests
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import Select
from selenium.webdriver.remote.webelement import WebElement
from dataclasses import dataclass
from locale import atof, setlocale, LC_NUMERIC
from typing import IO
import subprocess

TOR_PATH = "/usr/local/bin/tor"
TOR_PORT = 9050
GECKO_DRIVER_PATH = './geckodriver'

Percentage = float
DollarAmount = float
Year = int


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
    raw_units = []

    for detail in self.raw_listing['detailedInfo']['listingDetails']:
      if detail.get('name') == 'Unit Information':
        raw_units = detail['subCategories']

    units: list[Unit] = []
    for raw_unit in raw_units:
      beds = 0.0
      baths = 0.0
      for field in raw_unit['fields']:
        if 'Baths' in field['key']:
          pass
          if 'Half' in field['key']:
            baths += 0.5 * float(field['values'][0])
          else:
            baths += int(field['values'][0])
        elif 'Bedrooms' in field['key']:
          # beds = int(field['values'][0])
          # TODO revert
          beds = 0
      units.append(Unit(beds, baths))

    return units


def from_raw(raw: str) -> Listing:
  '''
  from_raw takes in the raw webpage returned by an indivudal compass multi-family
  listing and extracts the raw listing information from the javascript.
  '''
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

  def estimate(self, listing: Listing) -> list[RentEstimate]:
    estimates: list[RentEstimate] = []
    for unit in listing.units:
      try:
        # TODO: TOR process and the browser can both become contexts: https://twitter.com/POTUS/status/1469474907700477958
        # Sometimes if the TOR output node is known to Rentometer (or perhaps by some other mechanism),
        # Rentometer will say that your free search limit is reached and the "Analyze" button will be inactive.
        # Keep restarting TOR and loading up Rentometer until we get a page where we can actually hit "Analyze".
        analyze_button_disabled = True
        while analyze_button_disabled:
          try:
            self.browser.close()
            self.tor.kill()
          except AttributeError:
            # expect a AttributeError the first time this loops
            pass

          # Start TOR and wait for it to boot up.
          # TODO: add a timeout here.
          self.tor = subprocess.Popen(TOR_PATH, stdout=subprocess.PIPE)
          maybe_stdout = self.tor.stdout
          if maybe_stdout is None:
            raise Exception("stdout was None")
          stdout: IO[bytes] = maybe_stdout
          while True:
            line = stdout.readline()
            if b"100% (done): Done" in line:
              break

          # Set up a browser proxied through TOR.
          options = Options()
          options.headless = False  #TODO
          options.set_preference('network.proxy.type', 1)
          options.set_preference('network.proxy.socks', '127.0.0.1')
          options.set_preference('network.proxy.socks_port', TOR_PORT)
          self.browser = webdriver.Firefox(service=Service(self.geckodriver),
                                           options=options)

          # Connect to Rentometer and check if the analyze_button is disabled.
          self.browser.get("https://www.rentometer.com/")
          analyze_button = self.browser.find_element_by_name("commit")
          analyze_button_disabled = analyze_button.get_attribute(
              "disabled") == "true"

        # We have the enabled analyze_button, now get the other html elements we'll need.
        address_box = self.browser.find_element_by_id(
            "address_unified_search_address")
        beds_selector = Select(
            self.browser.find_element_by_id(
                "address_unified_search_bed_style"))
        baths_selector = Select(
            self.browser.find_element_by_id("address_unified_search_baths"))

        address_box.send_keys(listing.pretty_address)

        beds_selector.select_by_value(str(
            unit.beds))  # "1" = 1 bed, "2" = 2 beds, etc.

        # <option value="">Any</option>
        # <option value="1">1 Only</option>
        # <option value="1.5">1½ or more</option>
        if unit.baths == 1:
          baths_selector.select_by_value("1")
        elif unit.baths > 1:
          baths_selector.select_by_value("1.5")
        else:
          # TODO error
          pass
        # baths_selector.select_by_value("1.5")

        # Click analyze button to get analysis (typically opens a new page).
        analyze_button.click()

        # Check that rentometer was able to find enough results.
        try:
          if "Sorry, there are not enough results in that location to generate a valid analysis." in self.browser.find_element_by_xpath(
              "/html/body/div[3]/div").text:
            # TODO: error handling
            continue
        except NoSuchElementException:
          # This is the happy path.
          pass

        # Now we're on the analysis page.
        stats: list[WebElement] = self.browser.find_elements_by_class_name(
            "box-stats")
        average: DollarAmount
        median: DollarAmount
        twenty_fifth_percentile: DollarAmount
        seventy_fifth_percentile: DollarAmount
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
            print("Something got fucked")  #TODO throw an error

        estimates.append(
            RentEstimate(unit, average, median, twenty_fifth_percentile,
                         seventy_fifth_percentile))

      finally:
        # Close the browser and kill TOR.
        # self.browser.close()
        # self.proc.kill()
        pass

    return estimates


if __name__ == '__main__':
  page = requests.get(
      "https://www.compass.com/listing/689-auburn-street-manchester-nh-03103/932977102909516985/"
  )
  listing = from_raw(page.text)
  re = RentEstimator()
  estimates = re.estimate(listing)
  for estimate in estimates:
    print(estimate)
