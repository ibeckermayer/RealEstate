# ListingAnalyzer takes in a json of raw listings and produces a list of Listing, each of which
# contains a list of Scenario.
import json
from dataclasses import dataclass

Percentage = float
DollarAmount = float
Year = int


def load_from_file(filename: str) -> dict:
  with open(filename) as json_file:
    return json.load(json_file)


# expects a json in the format returned by
# https://www.zillow.com/search/GetSearchPageState.htm?searchQueryState=<query>
def extract_raw_listings(json: dict) -> list[dict]:
  return json["cat1"]["searchResults"]["listResults"] + json["cat1"][
      "searchResults"]["mapResults"]


# list[DollarAmount][n-1] = 50th percentile revenue for the nth month of the year
revenue_destin_50: dict[str, list[DollarAmount]] = {
    "1 Bedroom":
    [1122, 2452, 1870, 1853, 2310, 3881, 4400, 3123, 2812, 2790, 1409, 1007],
    "2 Bedroom":
    [1598, 3017, 2391, 1971, 3104, 5355, 6366, 3859, 3330, 3275, 1704, 1232],
    "3 Bedroom":
    [1998, 3113, 3430, 2691, 4490, 7744, 9340, 5777, 4985, 4751, 2720, 1836],
    "4 Bedroom":
    [2160, 3718, 5342, 4050, 6516, 11876, 13974, 8294, 6489, 6189, 3531, 2571]
}


@dataclass
class ScenarioParams:
  prices: list[DollarAmount]
  down_payments: list[Percentage]
  yearly_mortgage_rates: list[Percentage]
  closing_cost_rates: list[Percentage]
  immediate_repair_rates: list[Percentage]
  furnishing_costs: list[DollarAmount]
  monthly_utility_costs: list[DollarAmount]
  nightly_amenities_costs: list[DollarAmount]
  yearly_capex_rates: list[Percentage]
  yearly_maintenance_rates: list[Percentage]
  monthly_management_rates: list[Percentage]


class Listing(object):
  '''
  Listing wraps a raw listing and provides methods for calculating various
  estimated costs and earnings of the given property.
  '''
  raw_listing: dict

  def __init__(self, raw_listing: dict):
    self.raw_listing = raw_listing

  def get_price(self) -> DollarAmount:
    try:
      return DollarAmount(self.raw_listing["unformattedPrice"])
    except KeyError as e:
      return DollarAmount(
          self.raw_listing["hdpData"]["homeInfo"]["priceForHDP"])

  def calc_down_payment(self, percent_down: Percentage = 5) -> DollarAmount:
    return self.get_price() * (percent_down / 100)

    # Highly variable, 3% is standard rule of thumb.
  def calc_closing_cost(self, estimate: Percentage = 3) -> DollarAmount:
    return self.get_price() * (estimate / 100)

  # Highly variable, an old house will be far more than 2%. 5-10k is another rule of thumb for this price range.
  def calc_immediate_repairs(self, estimate: Percentage = 2) -> DollarAmount:
    return self.get_price() * (estimate / 100)

  # Highly variable rule of thumb for partially furnished place.
  def calc_furninshing_cost(self, cost: DollarAmount = 10000) -> DollarAmount:
    return cost

  def calc_monthly_mortgage_payment(
      self,
      yearly_rate: Percentage,
      down_payment: DollarAmount,
      mortgage_length: Year = 30) -> DollarAmount:

    # calculates the monthly mortgage payment based on the price of the home and rate/length of the mortgage.
    # M = p [ r(1 + r)^n ] / [ (1 + r)^n – 1]
    # M = monthly mortgage payment
    # p = the principal amount
    # r = your monthly interest rate. Your lender likely lists interest rates as an annual figure, so you’ll need to divide by 12, for each month of the year. So, if your rate is 5%, then the monthly rate will look like this: 0.05/12 = 0.004167.
    # n = the number of payments over the life-span of the loan. If you take out a 30-year fixed rate mortgage, this means:- n = 30 years x 12 months per year, or 360 payments.
    def _calc_monthly_payment(p: DollarAmount, yearly_rate: Percentage,
                              mortgage_length: Year) -> DollarAmount:
      n = mortgage_length * 12
      r = yearly_rate / 100.0 / 12.0

      return p * (r * (1 + r)**n) / ((1 + r)**n - 1)

    p = self.get_price() - down_payment

    return _calc_monthly_payment(p, yearly_rate, mortgage_length)

  def calc_monthly_utilities(self, cost: DollarAmount = 300) -> DollarAmount:
    return cost

  # Rod and Alicia are at $4/night for coffee, netflix, popcorn, shampoo, tp
  def calc_monthly_amenities(self,
                             occupancy_rate: Percentage = 100,
                             nightly_cost: DollarAmount = 4) -> DollarAmount:
    return (occupancy_rate / 100) * 30 * nightly_cost

  # Estimate of monthly cash to go towards big repairs
  def calc_monthly_capex(self, yearly_rate: Percentage = 1.25) -> DollarAmount:
    return self.get_price() * (yearly_rate / 100) / 12

  # Estimate of monthly cash to go towards small repairs and such
  def calc_monthly_maintenance(self,
                               yearly_rate: Percentage = 0.5) -> DollarAmount:
    return self.get_price() * (yearly_rate / 100) / 12

  # Calculate property taxes
  # .83 is FL average according to smartasset
  def calc_monthly_property_taxes(self,
                                  rate: Percentage = 0.83) -> DollarAmount:
    return self.get_price() * (rate / 100) / 12

  def calc_avg_monthly_revenue(self) -> DollarAmount:
    monthly_rev_list = revenue_destin_50[
        f"{int(self.raw_listing['hdpData']['homeInfo']['bedrooms'])} Bedroom"]
    avg_monthly_rev = sum(monthly_rev_list) / len(monthly_rev_list)
    return avg_monthly_rev

  def calc_airbnb_fee(self, avg_monthly_rev: DollarAmount) -> DollarAmount:
    return avg_monthly_rev * (3 / 100)

  def calc_monthly_management_fee(self,
                                  monthly_revenue: DollarAmount,
                                  rate: Percentage = 30) -> DollarAmount:
    return monthly_revenue * (rate / 100)

  def generate_scenarios(self, params: ScenarioParams):
    for price in params.prices:
      for down_payment in params.down_payments:
        for yearly_mortgage_rate in params.yearly_mortgage_rates:
          for closing_cost_rate in params.closing_cost_rates:
            for immediate_repair_rate in params.immediate_repair_rates:
              for furnishing_cost in params.furnishing_costs:
                for monthly_utility_cost in params.monthly_utility_costs:
                  for nightly_amenities_cost in params.nightly_amenities_costs:
                    for yearly_capex_rate in params.yearly_capex_rates:
                      for yearly_maintenance_rate in params.yearly_maintenance_rates:
                        for monthly_management_rate in params.monthly_management_rates:
                          _down_payment = listing.calc_down_payment(
                              down_payment)
                          _closing_cost = listing.calc_closing_cost(
                              closing_cost_rate)
                          _immediate_repairs = listing.calc_immediate_repairs(
                              immediate_repair_rate)
                          _furnishing_cost = listing.calc_furninshing_cost(
                              furnishing_cost)

                          print(f'For a home asking for: {price}')
                          print(
                              f'You would expect a down payment: ${_down_payment}'
                          )
                          print(
                              f'You would expect a closing cost: ${_closing_cost}'
                          )
                          print(
                              f'You would expect a immediate repairs: ${_immediate_repairs}'
                          )
                          print(
                              f'You would expect a furnishing cost: ${_furnishing_cost}'
                          )
                          _upfront_cost = _down_payment + _closing_cost + _immediate_repairs + _furnishing_cost
                          print(
                              f'For a total upfront cost of: ${_upfront_cost}')

                          # recurring (monthly)
                          # utilities -- $300
                          _utilities = listing.calc_monthly_utilities(
                              monthly_utility_cost)
                          print(f'Estimate monthly utilities at ${_utilities}')
                          # amenities -- $4/night for coffee, netflix, popcorn, shampoo, tp
                          _amenities = listing.calc_monthly_amenities(
                              100, nightly_amenities_cost)
                          print(f'Monthly amenities at ${_amenities}')
                          # big repairs (capex) -- 1.25% of the property value per year
                          _repairs = listing.calc_monthly_capex(
                              yearly_capex_rate)
                          print(f'Put aside ${_repairs} a month for repairs')
                          # small repairs (maintenance) -- 0.5% of the property value per year
                          _maintenance = listing.calc_monthly_maintenance(
                              yearly_maintenance_rate)
                          print(f'And ${_maintenance} a month for maintenance')
                          # hoa -- depends, but would eliminate the repairs and amenities potentially
                          # taxes -- find some programatic way to do it based on location
                          _taxes = listing.calc_monthly_property_taxes()
                          print(f'Paying monthly property taxes of ${_taxes}')
                          # mortgage
                          _mortgage = listing.calc_monthly_mortgage_payment(
                              yearly_mortgage_rate, down_payment)
                          print(
                              f'And a monthly mortgage payment of ${_mortgage}'
                          )
                          # total
                          _total_monthly_expenses = _utilities + _amenities + _repairs + _maintenance + _taxes + _mortgage
                          print(
                              f"For total monthly expenses of ${_total_monthly_expenses}"
                          )

                          # income
                          # https://theshorttermshop.com/emerald-coast-rental-data-2020/ (take off airbnb fee)
                          _avg_monthly_rev = listing.calc_avg_monthly_revenue()
                          print(
                              f"Then expect an average monthly revenue of ${_avg_monthly_rev}"
                          )

                          _airbnb_fee = listing.calc_airbnb_fee(
                              _avg_monthly_rev)
                          print(f"Of which {_airbnb_fee} (3%) goes to Airbnb.")

                          _monthly_mgmt_fee = listing.calc_monthly_management_fee(
                              _avg_monthly_rev, monthly_management_rate)
                          print(
                              f"And of which {monthly_management_rate}% goes to a management fee: ${_monthly_mgmt_fee}"
                          )

                          avg_monthly_profit = _avg_monthly_rev - _total_monthly_expenses - _airbnb_fee - _monthly_mgmt_fee
                          print(
                              f"For an average monthly profit of ${avg_monthly_profit}"
                          )
                          print(
                              f"Meaning you'd make all your money back in {_upfront_cost/avg_monthly_profit} months"
                          )
                          print()


if __name__ == '__main__':
  raw_listings = extract_raw_listings(load_from_file('east_of_pensacola.json'))
  for raw_listing in raw_listings:
    try:
      listing = Listing(raw_listing)
      params = ScenarioParams(prices=[listing.get_price()],
                              down_payments=[5],
                              yearly_mortgage_rates=[3.23],
                              closing_cost_rates=[3],
                              immediate_repair_rates=[3],
                              furnishing_costs=[10000],
                              monthly_utility_costs=[300],
                              nightly_amenities_costs=[4],
                              yearly_capex_rates=[1.25],
                              yearly_maintenance_rates=[0.5],
                              monthly_management_rates=[30])
      listing.generate_scenarios(params)
      # # up front
      # down_payment = listing.calc_down_payment(5)
      # closing_cost = listing.calc_closing_cost(3)
      # immediate_repairs = listing.calc_immediate_repairs(3)
      # furnishing_cost = listing.calc_furninshing_cost()

      # print(f'For a home asking for: {raw_listing["price"]}')
      # print(f'You would expect a down payment: ${down_payment}')
      # print(f'You would expect a closing cost: ${closing_cost}')
      # print(f'You would expect a immediate repairs: ${immediate_repairs}')
      # print(f'You would expect a furnishing cost: ${furnishing_cost}')
      # upfront_cost = down_payment + closing_cost + immediate_repairs + furnishing_cost
      # print(f'For a total upfront cost of: ${upfront_cost}')

      # # recurring (monthly)
      # # utilities -- $300
      # utilities = listing.calc_monthly_utilities()
      # print(f'Estimate monthly utilities at ${utilities}')
      # # amenities -- $4/night for coffee, netflix, popcorn, shampoo, tp
      # amenities = listing.calc_monthly_amenities()
      # print(f'Monthly amenities at ${amenities}')
      # # big repairs (capex) -- 1.25% of the property value per year
      # repairs = listing.calc_monthly_capex()
      # print(f'Put aside ${repairs} a month for repairs')
      # # small repairs (maintenance) -- 0.5% of the property value per year
      # maintenance = listing.calc_monthly_maintenance()
      # print(f'And ${maintenance} a month for maintenance')
      # # hoa -- depends, but would eliminate the repairs and amenities potentially
      # # taxes -- find some programatic way to do it based on location
      # taxes = listing.calc_monthly_property_taxes()
      # print(f'Paying monthly property taxes of ${taxes}')
      # # mortgage
      # mortgage = listing.calc_monthly_mortgage_payment(3.23, 30)
      # print(f'And a monthly mortgage payment of ${mortgage}')
      # # total
      # total_monthly_expenses = utilities + amenities + repairs + maintenance + taxes + mortgage
      # print(f"For total monthly expenses of ${total_monthly_expenses}")

      # # income
      # # https://theshorttermshop.com/emerald-coast-rental-data-2020/ (take off airbnb fee)
      # avg_monthly_rev = listing.calc_avg_monthly_revenue()
      # print(f"Then expect an average monthly revenue of ${avg_monthly_rev}")

      # airbnb_fee = listing.calc_airbnb_fee(avg_monthly_rev)
      # print(f"Of which {airbnb_fee} (3%) goes to Airbnb.")

      # mgmt_rate: Percentage = 30
      # monthly_mgmt_fee = listing.calc_monthly_management_fee(
      #     avg_monthly_rev, mgmt_rate)
      # print(
      #     f"And of which {mgmt_rate}% goes to a management fee: ${monthly_mgmt_fee}"
      # )

      # avg_monthly_profit = avg_monthly_rev - total_monthly_expenses - airbnb_fee - monthly_mgmt_fee
      # print(f"For an average monthly profit of ${avg_monthly_profit}")
      # print(
      #     f"Meaning you'd make all your money back in {upfront_cost/avg_monthly_profit} months"
      # )
      # print()
    except KeyError as key:
      # TODO
      # print(f'Recieved key error for key {key} on raw_listing below:')
      # print(raw_listing)
      # print()
      print('caught KeyError')
      print()
      continue

print(len(raw_listings))