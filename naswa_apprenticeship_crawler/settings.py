BOT_NAME = "naswa_apprenticeship_crawler"

SPIDER_MODULES = ["naswa_apprenticeship_crawler.spiders"]
NEWSPIDER_MODULE = "naswa_apprenticeship_crawler.spiders"

ROBOTSTXT_OBEY = True

# One request at a time, one second between requests
CONCURRENT_REQUESTS = 1
DOWNLOAD_DELAY = 1
RANDOMIZE_DOWNLOAD_DELAY = False
AUTOTHROTTLE_ENABLED = False

# Polite headers
USER_AGENT = "naswa-apprenticeship-crawler/0.1 (research; contact p.craig@bloomworks.digital)"

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"
FEED_EXPORT_ENCODING = "utf-8"
