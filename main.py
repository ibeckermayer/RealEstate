import json, logging, requests, subprocess, traceback, os, enum, time, pickle, inspect, re
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import Select
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from googleapiclient.discovery import build
from dataclasses import dataclass
from locale import atof, setlocale, LC_NUMERIC
from typing import IO, Tuple, Union, Optional
from gspread import service_account, Spreadsheet, Worksheet, WorksheetNotFound
from pprint import pprint
from constants import TOR_PATH, TOR_PORT, GECKO_DRIVER_PATH, GOOGLE_CREDENTIALS_FILE, REAL_ESTATE_FOLDER_ID, CACHEDIR, ESTIMATE_FILE
from types_ import Percentage, DollarAmount, SpreadsheetID
from utils import calc_monthly_mortgage_payment, calc_down_payment, gspread_retry, get_logger
from functools import cache, cached_property


@dataclass
class Unit:
  beds: float
  baths: float


class Listing:
  '''
  Listing is a single listing. You can optionally pass a list of units (i.e. if units can't be
  extracted from the raw_listing)
  '''
  def __init__(self, raw_listing: dict, units: list[Unit] = None):
    self.raw_listing = raw_listing
    self.logger = get_logger(self.pretty_address)
    self._units = units

    # The following line causes all @property's and @cached_property's to
    # evaluate immediately, which will cause any uknown data structures
    # in the raw_listing to throw an error immediately.
    inspect.getmembers(self)

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

  class IncongruentFormat(Warning):
    '''
    Warning to be thrown by the self._units_from* functions when they encounter something unexpected.
    This is a warning because such an incident doesn't mean an error occurred, it just means that the
    self._units_from* function failed and another one might still work.
    '''
    pass

  def _units_from_Unit_Information(self) -> list[Unit]:
    '''
    Extracts unit info from a detail in self.raw_listing['detailedInfo']['listingDetails'] that looks like:
    {'name': 'Unit Information', 'subCategories': [{'name': 'Unit 1', 'fields': [{'key': 'Unit 1 Baths', 'values': ['1']}, {'key': 'Unit 1 Bedrooms', 'values': ['4']}, {'key': 'Unit 1 Lease Term', 'values': ['Month to Month']}, {'key': 'Unit 1 Level Number', 'values': ['1']}, {'key': 'Unit 1 Rental Amt Freq', 'values': ['Monthly']}, {'key': 'Unit 1 Rental Amount', 'values': ['$1,399.00']}, {'key': 'Unit 1 Tenant Pays', 'values': ['Electric', 'Heat', 'Hot Water']}]}, {'name': 'Unit 2', 'fields': [{'key': 'Unit 2 Lease Term', 'values': ['Annual']}, {'key': 'Unit 2 Baths', 'values': ['1']}, {'key': 'Unit 2 Level Number', 'values': ['2']}, {'key': 'Unit 2 Rental Amt Freq', 'values': ['Monthly']}, {'key': 'Unit 2 Bedrooms', 'values': ['3']}, {'key': 'Unit 2 Rental Amount', 'values': ['$1,600.00']}, {'key': 'Unit 2 Tenant Pays', 'values': ['Electric', 'Heat', 'Hot Water']}]}]}

    If a detail with name 'Unit Information' can't be found or bed or bath information can't be extracted from any unit
    in precisely this format, it throws an IncongruentFromat warning.
    '''
    try:
      details = self.raw_listing['detailedInfo']['listingDetails']
    except KeyError as e:
      self.logger.debug(
          "Failed to find details from self.raw_listing['detailedInfo']['listingDetails']")
      raise self.IncongruentFormat()

    detail = next((d for d in details if d.get('name') == "Unit Information"), None)
    if detail is None:
      self.logger.debug(f"Failed to find a detail where detail['name'] == \"Unit Information\"")
      raise self.IncongruentFormat()

    sub_categories = detail.get('subCategories')
    if sub_categories is None:
      self.logger.debug(f"Failed because detail.get('subCategories') == None for detail = {detail}")
      raise self.IncongruentFormat()

    units: list[Unit] = []
    for sc in sub_categories:
      '''
      Looking for beds an dbaths in e.g.
      sc = {
            "name": "Unit 1",
            "fields": [
              { "key": "Unit 1 Baths", "values": ["1"] },
              { "key": "Unit 1 Bedrooms", "values": ["4"] },
              { "key": "Unit 1 Lease Term", "values": ["Month to Month"] },
              { "key": "Unit 1 Level Number", "values": ["1"] },
              { "key": "Unit 1 Rental Amt Freq", "values": ["Monthly"] },
              { "key": "Unit 1 Rental Amount", "values": ["$1,399.00"] },
              {
                "key": "Unit 1 Tenant Pays",
                "values": ["Electric", "Heat", "Hot Water"]
              }
            ]
          }
      '''
      name = sc.get('name')
      if name is None:
        self.logger.debug(f"Failed because sc.get('name') == None for sc = {sc}")
        raise self.IncongruentFormat()
      match = re.match("^Unit \d+", name)  # Look for "Unit 1", "Unit 2", etc.
      if match is None:
        self.logger.debug(
            f"Failed because match = re.match(\"^Unit \\d+\", name) == None for name = {name}")
        raise self.IncongruentFormat()
      unit_n_string = match.string  # This string is "Unit 1", "Unit 2", etc.
      fields = sc.get('fields')
      if not fields:
        self.logger.debug(f"sc.get('fields') == None for sub_category (sc) = {sc}")
        raise self.IncongruentFormat()
      # field = { "key": "Unit 1 Bedrooms", "values": ["4"] } should make beds_vals = ["4"]
      beds_vals = next((field.get("values")
                        for field in fields if field.get("key") == unit_n_string + " Bedrooms"),
                       None)
      baths_vals = next((field.get("values")
                         for field in fields if field.get("key") == unit_n_string + " Baths"), None)
      if beds_vals is None or baths_vals is None or len(beds_vals) != 1 or len(baths_vals) != 1:
        # If anything unexpected happened, we alert the caller that we didn't find our detail format.
        self.logger.debug(
            f"(beds_vals is None or baths_vals is None or len(beds_vals) != 1 or len(baths_vals) != 1) == True for beds_vals = {beds_vals}, baths_vals = {baths_vals}"
        )
        raise self.IncongruentFormat()

      beds = float(beds_vals[0])
      baths = float(baths_vals[0])
      units.append(Unit(beds, baths))

    return units

  def _units_from_Multi_Family(self) -> list[Unit]:
    '''
    Extracts unit info from a detail in self.raw_listing['detailedInfo']['listingDetails'] that looks like:
    {'name': 'Multi Family', 'subCategories': [{'name': 'Multi-Family Information', 'fields': [{'key': 'Unit Count', 'values': ['2']}, {'key': 'NUM OF UNITS', 'values': ['2']}]}, {'name': 'Income & Expenses Information', 'fields': [{'key': 'Gross Annual Income', 'values': ['$0']}, {'key': 'N.O.I. $', 'values': ['$.00']}, {'key': 'Annual Expenses', 'values': ['$0']}]}, {'name': 'Unit 1', 'fields': [{'key': 'Unit Rent Amount', 'values': ['$1,300.00']}, {'key': 'Unit Full Baths', 'values': ['1']}, {'key': 'Unit Bedrooms', 'values': ['2']}, {'key': 'Unit Available Date', 'values': ['08-31-2021 08:00:00 PM']}, {'key': 'Unit Deposit Amount', 'values': ['$.00']}, {'key': 'Unit Features', 'values': ['Deck', 'Dishwasher', 'Hardwood Floors', 'Range/Oven']}, {'key': 'Unit Garbage Included', 'values': ['Yes']}, {'key': 'Unit Gas Included', 'values': ['No']}, {'key': 'Unit Half Baths', 'values': ['0']}, {'key': 'Unit Maintenance Included', 'values': ['No']}, {'key': 'Unit Occupant', 'values': ['Avail In 1-3 Mon']}, {'key': 'Unit Water Included', 'values': ['No']}, {'key': 'Unit Electric Included', 'values': ['No']}]}, {'name': 'Unit 2', 'fields': [{'key': 'Unit Rent Amount', 'values': ['$1,300.00']}, {'key': 'Unit Full Baths', 'values': ['1']}, {'key': 'Unit Bedrooms', 'values': ['2']}, {'key': 'Unit Available Date', 'values': ['08-31-2021 08:00:00 PM']}, {'key': 'Unit Deposit Amount', 'values': ['$1,300.00']}, {'key': 'Unit Features', 'values': ['Deck', 'Dishwasher', 'Hardwood Floors', 'Range/Oven']}, {'key': 'Unit Garbage Included', 'values': ['Yes']}, {'key': 'Unit Gas Included', 'values': ['No']}, {'key': 'Unit Half Baths', 'values': ['0']}, {'key': 'Unit Maintenance Included', 'values': ['No']}, {'key': 'Unit Occupant', 'values': ['Avail In 1-3 Mon']}, {'key': 'Unit Water Included', 'values': ['No']}, {'key': 'Unit Electric Included', 'values': ['No']}]}]}

    If a detail with name 'Unit Information' can't be found or bed or bath information can't be extracted from any unit
    in precisely this format, it throws an IncongruentFromat warning.
    '''
    try:
      details = self.raw_listing['detailedInfo']['listingDetails']
    except KeyError as e:
      self.logger.debug(
          "Failed to find details from self.raw_listing['detailedInfo']['listingDetails']")
      raise self.IncongruentFormat()

    detail = next((d for d in details if d.get('name') == "Multi Family"), None)
    if detail is None:
      self.logger.debug(
          f"Failed to find a detail where detail['name'] == \"Multi Family\" in details = {details}"
      )
      raise self.IncongruentFormat()

    sub_categories = detail.get('subCategories')
    if sub_categories is None:
      self.logger.debug(f"Failed because detail.get('subCategories') == None for detail = {detail}")
      raise self.IncongruentFormat()

    units: list[Unit] = []
    for sc in sub_categories:
      '''
      Looking for beds an dbaths in e.g.
      sc = {
              "name": "Unit 1",
              "fields": [
                { "key": "Unit Rent Amount", "values": ["$1,300.00"] },
                { "key": "Unit Full Baths", "values": ["1"] },
                { "key": "Unit Bedrooms", "values": ["2"] },
                {
                  "key": "Unit Available Date",
                  "values": ["08-31-2021 08:00:00 PM"]
                },
                { "key": "Unit Deposit Amount", "values": ["$.00"] },
                {
                  "key": "Unit Features",
                  "values": ["Deck", "Dishwasher", "Hardwood Floors", "Range/Oven"]
                },
                { "key": "Unit Garbage Included", "values": ["Yes"] },
                { "key": "Unit Gas Included", "values": ["No"] },
                { "key": "Unit Half Baths", "values": ["0"] },
                { "key": "Unit Maintenance Included", "values": ["No"] },
                { "key": "Unit Occupant", "values": ["Avail In 1-3 Mon"] },
                { "key": "Unit Water Included", "values": ["No"] },
                { "key": "Unit Electric Included", "values": ["No"] }
              ]
            },
      '''
      name = sc.get('name')
      if name is None:
        self.logger.debug(f"Failed because sc.get('name') == None for sc = {sc}")
        raise self.IncongruentFormat()
      match = re.match("^Unit \d+", name)  # Look for "Unit 1", "Unit 2", etc.
      if match is None:
        # It's valid for some subcategories not to match, if we encounter those just continue.
        self.logger.debug(
            f're.match("^Unit \\d+", name) failed for name = {name}; not an error, continuing')
        continue
      fields = sc.get('fields')
      if not fields:
        self.logger.debug(f"sc.get('fields') == None for sub_category (sc) = {sc}")
        raise self.IncongruentFormat()
      # field = { "key": "Unit 1 Bedrooms", "values": ["4"] } should make beds_vals = ["4"]
      beds_vals = next(
          (field.get("values") for field in fields if field.get("key") == "Unit Bedrooms"), None)
      baths_vals = next(
          (field.get("values") for field in fields if field.get("key") == "Unit Full Baths"), None)
      half_baths_vals = next(
          (field.get("values") for field in fields if field.get("key") == "Unit Half Baths"), None)
      if beds_vals is None or baths_vals is None or half_baths_vals is None or len(
          beds_vals) != 1 or len(baths_vals) != 1 or len(half_baths_vals) != 1:
        # If anything unexpected happened, we alert the caller that we didn't find our detail format.
        self.logger.debug(
            f"(beds_vals is None or baths_vals is None or half_baths_vals is None or len(beds_vals) != 1 or len(baths_vals) != 1  or len(half_baths_vals) != 1 for beds_vals = {beds_vals}, baths_vals = {baths_vals}, half_baths_vals = {half_baths_vals}"
        )
        raise self.IncongruentFormat()

      beds = float(beds_vals[0])
      baths = float(baths_vals[0]) + float(half_baths_vals[0])
      units.append(Unit(beds, baths))

    return units

  @cached_property
  def units(self) -> list[Unit]:
    '''
    Tries to extract unit information from the raw listing by trying each of the known formats.
    If the raw listing is incongruent with any of the known formats, raises an exception.
    '''
    if self._units:
      self.logger.debug(f'Skipping unit extraction and using the passed in units: {self._units}')
      return self._units

    units: Optional[list[Unit]] = None
    for unit_extractor in [self._units_from_Unit_Information, self._units_from_Multi_Family]:
      try:
        units = unit_extractor()
        break
      except self.IncongruentFormat:
        continue
    if units is None or len(units) == 0:
      self.logger.debug(f"Unit extraction failed for self.raw_listing = {self.raw_listing}")
      raise Exception("Failed to find a known format for extracting unit info")

    self.logger.debug(f"Successfully extracted units: {units}")

    return units


def from_raw(raw: str, units: list[Unit] = None) -> Listing:
  '''
  from_raw takes in the raw webpage returned by an indivudal compass multi-family
  listing and extracts the raw listing information from the javascript.
  '''
  logging.info("Extracting raw listing")
  return Listing(
      json.loads(raw.split("window.__PARTIAL_INITIAL_DATA__ = ")[1].split("</script>")[0].strip())
      ['props']['listingRelation']['listing'], units)


class EstimateType(enum.Enum):
  '''
  Descriptor of what type of estimate some estimate is.
  '''
  AVERAGE = 'average'
  MEDIAN = 'median'
  PERCENTILE_25 = "25th percentile"
  PERCENTILE_75 = "75th percentile"


@dataclass
class RentEstimatedUnit:
  unit: Unit
  monthly_rent: DollarAmount


@dataclass
class RentEstimate:
  units: list[RentEstimatedUnit]
  type: EstimateType


class RentEstimator(object):
  '''
  RentEstimator uses selenium (ff proxied through TOR) to query https://www.rentometer.com/ for a rent estimate.
  geckodriver is the path to the Gecko driver binary (https://github.com/mozilla/geckodriver/releases).
  '''
  def __init__(
      self,
      logger: logging.Logger,
      geckodriver: str = GECKO_DRIVER_PATH,
  ):
    self.logger = logger
    self.browser: webdriver.Firefox
    self.tor: subprocess.Popen[bytes]
    self.geckodriver = geckodriver
    self.estimates: list[RentEstimate]

  # TODO: could become a RentometerBrowser class
  def _get_unthrottled_tor_browser(self) -> Tuple[subprocess.Popen[bytes], webdriver.Firefox]:
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
      self.logger.info("Starting TOR...")
      # Start TOR and wait for it to boot up.
      # TODO: add a timeout here.
      tor = subprocess.Popen(TOR_PATH, stdout=subprocess.PIPE)
      maybe_stdout = tor.stdout
      if maybe_stdout is None:
        raise Exception("stdout was None")
      stdout: IO[bytes] = maybe_stdout
      while True:
        line = stdout.readline()
        self.logger.debug(f"TOR startup output: {'{!r}'.format(line)}")
        if b"100% (done): Done" in line:
          break
      self.logger.info(f"TOR started successfully")

      # Create a TOR browser
      self.logger.info(f"Opening browser...")
      browser = webdriver.Firefox(service=Service(self.geckodriver),
                                  options=options,
                                  capabilities=capabilities)

      # Connect to Rentometer and check if the analyze_button is disabled.
      self.logger.info(f"Connecting to https://www.rentometer.com/")
      browser.get("https://www.rentometer.com/")
      self.logger.info(f"https://www.rentometer.com/ connection succeeded")
      self.logger.info(f"Checking if the \"Analyze\" button is enabled")
      analyze_button = browser.find_element_by_name("commit")
      analyze_button_disabled = analyze_button.get_attribute("disabled") == "true"

      # If the analyze button is disabled, kill the browser and TOR and try again.
      if analyze_button_disabled:
        self.logger.warning(
            "Opened Rentometer \"Analyze\" button was disabled. Killing the browser and TOR and trying again."
        )
        browser.close()
        tor.kill()
      else:
        self.logger.info("Got a Rentometer browser with the \"Analyze\" button enabled.")

    return (tor, browser)

  def _nuke_tor_browser(self):
    # Close the browser and kill TOR.
    self.logger.info("Closing the browser and killing TOR")
    try:
      self.browser.quit()
    except AttributeError:
      # self.browser wasn't set
      pass
    try:
      self.tor.kill()
    except AttributeError:
      # self.tor wasn't set
      pass

  def estimate(self, listing: Listing) -> list[RentEstimate]:
    self.logger.info(f"Estimating the rents at {listing.pretty_address}")

    estimate_cache_dir = os.path.join(CACHEDIR, listing.pretty_address)
    estimate_cache_file = os.path.join(estimate_cache_dir, ESTIMATE_FILE)
    self.logger.info(f"Checking for cached estimates at {estimate_cache_file}")
    try:
      with open(estimate_cache_file, 'rb') as f:
        self.estimates = pickle.load(f)
        self.logger.info(
            f"Found cached estimates for {listing.pretty_address}, using those for analysis")
        return self.estimates
    except FileNotFoundError:
      self.logger.info(
          f"Could not find cached estimates for {listing.pretty_address}, scraping rentometer")

    self.estimates = []

    def add_estimate(estimate_type: EstimateType, unit: Unit, monthly_rent: DollarAmount):
      '''
      Helper function for adding a RentEstimatedUnit to the self.estimates
      '''
      # Get the estimate for this type if it already exists, or create a new one
      estimate = next((e for e in self.estimates if e.type == estimate_type),
                      RentEstimate([], estimate_type))
      # Append the RentEstimatedUnit
      estimate.units.append(RentEstimatedUnit(unit, monthly_rent))
      # If we created a new estimate above, append it to self.estimates.
      # Otherwise, we already updated the estimate in self.estimates so we
      # don't need to do anything.
      if estimate not in self.estimates:
        self.estimates.append(estimate)

    try:
      self.tor, self.browser = self._get_unthrottled_tor_browser()

      def enter_listing_info_and_click_analyze(pretty_address: str, beds: float, baths: float):
        '''
        baths <= 0 will select "Any" for the number of bathrooms
        '''
        # Find the relevant UI elements
        analyze_button = self.browser.find_element_by_name("commit")
        if analyze_button.get_attribute("disabled") == "true":
          # If rentometer is throttling our Analyze requests, kill the browser and TOR and reboot them until we can analyze again.
          self._nuke_tor_browser()
          self.tor, self.browser = self._get_unthrottled_tor_browser()

        address_box = self.browser.find_element_by_id("address_unified_search_address")
        beds_selector = Select(self.browser.find_element_by_id("address_unified_search_bed_style"))
        baths_selector = Select(self.browser.find_element_by_id("address_unified_search_baths"))

        address_box.send_keys(pretty_address)
        self.logger.info(f"Entered {pretty_address} into the address box")

        beds_selector.select_by_value(str(int(beds)))  # "1" = 1 bed, "2" = 2 beds, etc.
        self.logger.info(f"Selected {beds} for \"Beds\"")

        # <option value="">Any</option>
        # <option value="1">1 Only</option>
        # <option value="1.5">1½ or more</option>
        if baths == 1:
          baths_selector.select_by_value("1")
          self.logger.info(f"Selected \"1 Only\" for \"Baths\"")
        elif baths > 1:
          baths_selector.select_by_value("1.5")
          self.logger.info(f"Selected \"1½ or more\" for \"Baths\"")
        else:
          baths_selector.select_by_value("")
          self.logger.info(f"Selected \"Any\" for \"Baths\"")

        # Click analyze button to get analysis (typically opens a new page).
        self.logger.info("Clicking the analyze button")
        analyze_button.click()

      class NotEnoughResults(Exception):
        pass

      def check_not_enough_results():
        '''
        raises NotEnoughResults if we get the "sorry not enough results" warning from rentometer,
        returns otherwise
        '''
        try:
          if "Sorry, there are not enough results in that location to generate a valid analysis." in self.browser.find_element_by_xpath(
              "/html/body/div[3]/div").text:
            self.logger.warning(
                f"Rentometer says: \"Sorry, there are not enough results in that location to generate a valid analysis.\" for {unit}"
            )
            self.logger.info("Retrying with \"Any\" for the number of bathrooms")
            # TODO all the above logic should be wrapped in a try logic, if the exception that will be thrown here is caught then
            # we should retry with "Any" baths.
            raise NotEnoughResults
        except NoSuchElementException:
          # This is the happy path.
          self.logger.info("Analysis succeeded")

      for unit in listing.units:
        self.logger.info(f"Estimating rent for unit: {unit}")
        enter_listing_info_and_click_analyze(listing.pretty_address, unit.beds, unit.baths)
        # Check that rentometer was able to find enough results.
        try:
          check_not_enough_results()
        except NotEnoughResults:
          self.logger.info("Retrying with \"Any\" for the number of bathrooms")
          enter_listing_info_and_click_analyze(listing.pretty_address, unit.beds, -1)
          try:
            check_not_enough_results()
          except NotEnoughResults:
            # This is the happy path.
            self.logger.error("Analysis failed with \"Any\" as the number of bathrooms")
            self.logger.debug(
                "Setting the rent estimates for this unit at 0 and continuing with analysis")
            for estimate_type in [
                EstimateType.AVERAGE, EstimateType.MEDIAN, EstimateType.PERCENTILE_25,
                EstimateType.PERCENTILE_75
            ]:
              add_estimate(estimate_type, unit, 0)
              continue

        # Now we're on the analysis page.
        stats: list[WebElement] = self.browser.find_elements_by_class_name("box-stats")
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
            add_estimate(EstimateType.AVERAGE, unit, average)
          elif 'MEDIAN' in stat.text:
            median = extract_dollar_value(stat)
            add_estimate(EstimateType.MEDIAN, unit, median)
          elif '25TH PERCENTILE' in stat.text:
            twenty_fifth_percentile = extract_dollar_value(stat)
            add_estimate(EstimateType.PERCENTILE_25, unit, twenty_fifth_percentile)
          elif '75TH PERCENTILE' in stat.text:
            seventy_fifth_percentile = extract_dollar_value(stat)
            add_estimate(EstimateType.PERCENTILE_75, unit, seventy_fifth_percentile)
          else:
            self.logger.warning(f"Unexpected stat in stats box: {stat.text}")

        if average == 0 or median == 0 or twenty_fifth_percentile == 0 or seventy_fifth_percentile == 0:
          self.logger.warning(
              f"Could not extract at least one stat from stats box: {[s.text for s in stats]}")
    except Exception:
      self.logger.error(
          f"Unexpected error encountered while creating rent estimate: {traceback.format_exc()}")
    finally:
      # Cache estimates if they were made
      if self.estimates:
        if not os.path.exists(estimate_cache_dir):
          os.mkdir(estimate_cache_dir)
        with open(estimate_cache_file, 'wb') as f:
          self.logger.info(f"Caching estimates at {estimate_cache_file}")
          pickle.dump(self.estimates, f)
      self._nuke_tor_browser()

    return self.estimates


@dataclass
class ScenarioParams:
  # Upfront expenses
  prices: list[DollarAmount]
  down_payment_rates: list[Percentage]
  closing_cost_rates: list[Percentage]
  immediate_repair_rates: list[Percentage]
  furnishing_costs: list[DollarAmount]

  # Ongoing expenses
  yearly_mortgage_rates: list[Percentage]
  monthly_utility_costs: list[DollarAmount]
  yearly_capex_rates: list[Percentage]
  yearly_maintenance_rates: list[Percentage]
  monthly_management_rate: Percentage
  monthly_property_taxes: DollarAmount
  monthly_hoa_fees: DollarAmount

  # Ongoing incomes
  rent_estimates: list[RentEstimate]


class SpreadsheetBuilder(object):
  '''
  Uses Google Sheets API to create and/or edit a spreadsheet.

  GOOGLE SHEETS API Read/Write Requests:
  - 60 / user / minute
  - 300 / minute
  - ∞ / day
  '''
  def __init__(self,
               name: str,
               params: ScenarioParams,
               logger: logging.Logger,
               cred_file=GOOGLE_CREDENTIALS_FILE):
    self.sh: Spreadsheet
    self.worksheet: Worksheet  # active worksheet

    self.name = name
    self.params = params
    self.row = 1  # row to write to
    self.sheet_num = 0  # uid for each worksheet

    self.logger = logger

    # _label_cache is used to keep track of which cells contain which values, indexed by their label.
    # For example, if self.next_row = 3, calling self.write_tuple("furniture", 700) will add an entry
    # in the next row of the Spreadsheet that looks like:
    #
    #      A3        B3
    # ---------------------
    # |furniture |   700  |
    # ---------------------
    #
    # And the tuple cache will get a new entry:
    # {
    #   'furniture': 'B3'
    # }
    #
    # Then later you can find the cell again using the _label_cache like:
    # self.write_tuple("double furniture", f"=2 * {self._label_cache["furniture"]}")
    self._label_cache: dict[str, str] = {}

    # Google api's use this as the default credential if no other is provided.
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = cred_file
    # Find existing or create a new spreadsheet.
    key = self._find_spreadsheet(name)
    if key == "":
      key = self._create_spreadsheet(name)

    client = service_account(filename=GOOGLE_CREDENTIALS_FILE)
    self.sh = client.open_by_key(key)
    self.worksheet = self.sh.sheet1  # default to sheet1

  def _find_spreadsheet(self, name: str) -> SpreadsheetID:
    '''
    Finds a Google Spreadsheet by name, returning the SpreadsheetID. If no Spreadsheet of the
    given name is found, returns an empty string.
    '''
    self.logger.info(f'Searching for a Spreadsheet named {name}')
    service = build('drive', 'v3')
    # q=f"'{REAL_ESTATE_FOLDER_ID}' in parents" searches only for files in the folder with id=REAL_ESTATE_FOLDER_ID.
    res = service.files().list(pageSize=1000, q=f"'{REAL_ESTATE_FOLDER_ID}' in parents").execute()
    files = res.get('files', [])
    if not files:
      self.logger.warning(f'Spreadsheet named {name} was not found')
      return ''
    for file in files:
      if file.get('name') == name:
        id = file['id']
        self.logger.info(f'Spreadsheet named {name} was found with id {id}')
        return id
    self.logger.warning(f'Spreadsheet named {name} was not found')
    return ''

  def _create_spreadsheet(self, name: str, parent=REAL_ESTATE_FOLDER_ID) -> SpreadsheetID:
    '''
    Creates a new Google Spreadsheet under an existing parent folder,
    returning the id of the newly created spreadsheet on success.
    '''
    self.logger.info(f"Creating new Spreadsheet named {name}")
    service = build('drive', 'v3')
    res = service.files().create(body={
        'name': name,
        'parents': [parent],
        'mimeType': 'application/vnd.google-apps.spreadsheet'
    }).execute()

    id = res.get('id')
    if id is None:
      raise Exception(
          f"Received unexpected Google Drive API response when attempting to create a new Spreadsheet: {res}"
      )

    self.logger.info(f"Created new Spreadsheet named {name} with id {id}")

    return id

  @gspread_retry
  def _get_or_create_worksheet(self, name: str):
    '''
    Gets a worksheet (if a worksheet by the given name exists) or creates a new one and sets the SpreadsheetBuilder to write to it.
    '''
    try:
      self.logger.info(f"Trying to switch to worksheet {name}")
      self.worksheet = self.sh.worksheet(name)
    except WorksheetNotFound:
      self.logger.info(f"Worksheet {name} not found, creating it instead")
      self.worksheet = self.sh.add_worksheet(name, 1000, 26)
    self.row = 1

  @gspread_retry
  def _write_tuple(self, label: str, value: Union[str, Percentage, DollarAmount]):
    self.logger.info(f"Writing tuple {label}, {value}")
    A = f"A{self.row}"
    B = f"B{self.row}"
    self.worksheet.update(A, [[label, str(value)]], raw=False)
    self._label_cache[label] = B
    self.row += 1

  def skip_line(self):
    self.logger.info("Skipping line")
    self.row += 1

  def get_label_cell(self, label: str) -> str:
    return self._label_cache[label]

  def build_spreadsheet(self):
    for price in self.params.prices:
      for down_payment_rate in self.params.down_payment_rates:
        for closing_cost_rate in self.params.closing_cost_rates:
          for immediate_repair_rate in self.params.immediate_repair_rates:
            for furnishing_cost in self.params.furnishing_costs:
              for yearly_mortgage_rate in self.params.yearly_mortgage_rates:
                for monthly_utility_cost in self.params.monthly_utility_costs:
                  for yearly_capex_rate in self.params.yearly_capex_rates:
                    for yearly_maintenance_rate in self.params.yearly_maintenance_rates:
                      for rent_estimate in self.params.rent_estimates:
                        self._get_or_create_worksheet(str(self.sheet_num))

                        # Upfront costs
                        self._write_tuple("Upfront Costs", "")
                        self._write_tuple("Price ($)", price)
                        self._write_tuple("Down (%)", down_payment_rate)
                        self._write_tuple("Closing cost rate (%)", closing_cost_rate)
                        self._write_tuple("Immediate repair rate (%)", immediate_repair_rate)
                        self._write_tuple(
                            "Down Payment ($)",
                            f'={self.get_label_cell("Price ($)")}*({self.get_label_cell("Down (%)")}/100)'
                        )
                        self._write_tuple(
                            "Closing costs ($)",
                            f'={self.get_label_cell("Price ($)")}*({self.get_label_cell("Closing cost rate (%)")}/100)'
                        )
                        self._write_tuple(
                            "Immediate repairs ($)",
                            f'={self.get_label_cell("Price ($)")}*({self.get_label_cell("Immediate repair rate (%)")}/100)'
                        )
                        self._write_tuple("Furnishing costs ($)", str(furnishing_cost))
                        self._write_tuple(
                            "TOTAL UPFRONT ($)",
                            f'={self.get_label_cell("Down Payment ($)")}+{self.get_label_cell("Closing costs ($)")}+{self.get_label_cell("Immediate repairs ($)")}+{self.get_label_cell("Furnishing costs ($)")}'
                        )

                        # Rent estimates
                        self.skip_line()
                        self._write_tuple(f"Rents ({str(rent_estimate.type)})", "")
                        gross_monthly_income_formula = "="
                        for i in range(len(rent_estimate.units)):
                          unit = rent_estimate.units[i].unit
                          est = rent_estimate.units[i].monthly_rent
                          label = f"Unit {i} ({unit.beds} beds, {unit.baths} baths)"
                          self._write_tuple(label, est)
                          gross_monthly_income_formula += self.get_label_cell(label)
                          if i < len(rent_estimate.units) - 1:
                            gross_monthly_income_formula += '+'
                        self._write_tuple("GROSS MONTHLY INCOME (RENT)",
                                          gross_monthly_income_formula)

                        # Monthly expenses
                        self.skip_line()
                        down_payment = calc_down_payment(price, down_payment_rate)
                        self._write_tuple("Mortgage rate (%)", yearly_mortgage_rate)
                        self._write_tuple(
                            "Total loan amount ($)",
                            f'={self.get_label_cell("Price ($)")} - {self.get_label_cell("Down Payment ($)")}'
                        )
                        self._write_tuple("Capex rate (%, yearly)", yearly_capex_rate)
                        self._write_tuple("Maintenance rate (%, yearly)", yearly_maintenance_rate)
                        self._write_tuple("Management rate (%, monthly)",
                                          self.params.monthly_management_rate)
                        self._write_tuple(
                            "Mortgage payment ($, monthly)",
                            calc_monthly_mortgage_payment(price=price,
                                                          yearly_rate=yearly_mortgage_rate,
                                                          down_payment=down_payment))
                        self._write_tuple(
                            "Average capex ($, monthly)",
                            f'={self.get_label_cell("Price ($)")}*({self.get_label_cell("Capex rate (%, yearly)")} / 100) / 12'
                        )
                        self._write_tuple(
                            "Average maintenance ($, monthly)",
                            f'={self.get_label_cell("Price ($)")}*({self.get_label_cell("Maintenance rate (%, yearly)")} / 100) / 12'
                        )
                        self._write_tuple(
                            "Management fee ($, monthly)",
                            f'={self.get_label_cell("GROSS MONTHLY INCOME (RENT)")} * ({self.get_label_cell("Management rate (%, monthly)")} / 100)'
                        )
                        self._write_tuple("Utilities ($, monthly)", monthly_utility_cost)
                        self._write_tuple("Property taxes ($, monthly)",
                                          self.params.monthly_property_taxes)
                        self._write_tuple("HOA fees ($, monthly)", self.params.monthly_hoa_fees)
                        self._write_tuple(
                            "Mortgageless Monthly Expenses",
                            f'={self.get_label_cell("Average capex ($, monthly)")} + {self.get_label_cell("Average maintenance ($, monthly)")} + {self.get_label_cell("Management fee ($, monthly)")} + {self.get_label_cell("Utilities ($, monthly)")} + {self.get_label_cell("Property taxes ($, monthly)")} + {self.get_label_cell("HOA fees ($, monthly)")}'
                        )
                        self._write_tuple(
                            "Mortgageless Expenses / Rents (\"50% rule\"?)",
                            f'={self.get_label_cell("Mortgageless Monthly Expenses")} / {self.get_label_cell("GROSS MONTHLY INCOME (RENT)")}'
                        )
                        self._write_tuple(
                            "TOTAL MONTHLY EXPENSES",
                            f'={self.get_label_cell("Mortgage payment ($, monthly)")} + {self.get_label_cell("Mortgageless Monthly Expenses")}'
                        )

                        self.skip_line()
                        self._write_tuple("Bottom Line", "")
                        self._write_tuple(
                            "NET MONTHLY INCOME",
                            f'={self.get_label_cell("GROSS MONTHLY INCOME (RENT)")} - {self.get_label_cell("TOTAL MONTHLY EXPENSES")}'
                        )

                        self.sheet_num += 1


@dataclass
class Input:
  url: str
  units: Optional[list[Unit]] = None


if __name__ == '__main__':
  logger = get_logger()
  startTime = time.time()
  s: SpreadsheetBuilder
  inputs: list[Input] = [
      # "https://www.compass.com/listing/1050-palafox-drive-northeast-atlanta-ga-30324/845726107540165857/",
      # "https://www.compass.com/listing/689-auburn-street-manchester-nh-03103/932977102909516985/",
      # "https://www.compass.com/listing/315-chateau-drive-southeast-rome-ga-30161/938535030264973529/"
      Input(
          "https://www.compass.com/listing/265-vistawood-drive-northeast-marietta-ga-30066/953808184193214265/",
          [Unit(2, 1), Unit(1, 1)])
  ]
  for input in inputs:
    try:
      page = requests.get(input.url)
      listing = from_raw(page.text, input.units)
      logger = get_logger(listing.pretty_address)
      logger.info(f"Units at {listing.pretty_address}: {listing.units}")

      rent_estimator = RentEstimator(logger)
      estimates = rent_estimator.estimate(listing)
      s = SpreadsheetBuilder(
          listing.pretty_address,
          ScenarioParams(
              # Upfront expenses
              prices=[listing.price],
              down_payment_rates=[5],
              closing_cost_rates=[3],
              immediate_repair_rates=[3],
              furnishing_costs=[10000],

              # Recurring income
              rent_estimates=[e for e in estimates if e.type == EstimateType.AVERAGE],
              # rent_estimates=estimates,

              # Recurring expenses
              yearly_mortgage_rates=[3.23],
              monthly_utility_costs=[300],
              yearly_capex_rates=[1.25],
              yearly_maintenance_rates=[0.5],
              monthly_management_rate=10,
              monthly_property_taxes=0,  # TODO
              monthly_hoa_fees=0,  # TODO
          ),
          logger)
      s.build_spreadsheet()
    except Exception as e:
      logger.error(e)
      continue

  executionTime = (time.time() - startTime)
  pprint('Execution time in seconds: ' + str(executionTime))
  exit(0)
