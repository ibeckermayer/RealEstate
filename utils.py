import time, logging, json
from typing import Callable
from types_ import Percentage, DollarAmount, Year
from gspread.exceptions import APIError


def calc_monthly_mortgage_payment(price: DollarAmount,
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

  p = price - down_payment

  return _calc_monthly_payment(p, yearly_rate, mortgage_length)


def calc_down_payment(price: DollarAmount, percent_down: Percentage) -> DollarAmount:
  return price * (percent_down / 100)


def gspread_retry(func: Callable) -> Callable:
  '''
  Throttling approach of hitting the Google Sheets api until we get a 429, then waiting a minute before continuing.
  '''
  INTERVAL_SECS = 0

  def wrapper(*args, **kwargs):
    try:
      func(*args, **kwargs)
    except APIError as e:
      if "'code': 429" in str(e):
        logging.warning(f"Google Sheets API limit reached, pausing for {INTERVAL_SECS}")
        time.sleep(INTERVAL_SECS)
        wrapper(*args, **kwargs)
      else:
        raise e

  return wrapper