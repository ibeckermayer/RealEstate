import time, logging, os, sys
from typing import Callable, cast
from types_ import Percentage, DollarAmount, Year
from gspread.exceptions import APIError
from constants import LOGSDIR, LOGFILE, LOGFMT


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


def get_logger(pretty_address: str = "root") -> logging.Logger:
  '''
  Gets a logger by the given name (pretty_address) or creates it if it doesn't exist, with this application's preferred logger settings.
  If pretty_address is "root", returns the root logger logging to stdout and LOGSDIR/root.
  '''
  _existing_log = logging.Logger.manager.loggerDict.get(pretty_address)
  if _existing_log is not None and type(_existing_log) is not logging.PlaceHolder:
    existing_log = cast(logging.Logger, _existing_log)
    return existing_log

  # logging.getLogger("") returns the root logger
  logger = logging.getLogger(pretty_address)
  logdir = os.path.join(LOGSDIR, pretty_address)

  if not os.path.exists(logdir):
    os.makedirs(logdir)
  logfile = os.path.join(logdir, LOGFILE)

  formatter = logging.Formatter(LOGFMT)
  fileHandler = logging.FileHandler(logfile)
  fileHandler.setFormatter(formatter)

  logger.addHandler(fileHandler)
  logger.setLevel(logging.DEBUG)  # TODO: make configurable

  # The root logger should still print everything to the console.
  if pretty_address == "root":
    stderrHandler = logging.StreamHandler(sys.stderr)
    stderrHandler.setFormatter(formatter)
    logger.addHandler(stderrHandler)

  return logger