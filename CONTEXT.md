# Flights Tracker Domain

This context describes discovering, comparing, and verifying flight options from
a traveller's stated preferences.

## Language

**Travel intent**:
Subjective preferences expressed by a traveller, such as "somewhere warm", "a
city break", or "a bargain".
_Avoid_: Filter, query

**Search criterion**:
An objective condition used to include, exclude, or rank flight options, such as
dates, stay length, budget, or direct-flight preference.
_Avoid_: Travel intent, preference

**Date scope**:
The degree of date flexibility in exploration: exact travel dates, calendar
months, or anytime.
_Avoid_: Flexible dates

**Destination scope**:
The set of destinations considered in flight discovery: one place, a defined
group of places, or anywhere.
_Avoid_: Destination filter

**Exploration**:
Broad discovery of candidate destinations or dates from indicative prices,
before selecting any candidate for live verification.
_Avoid_: Search, live search

**Origin option**:
One alternative departure place and its indicative prices for an explored
destination.
_Avoid_: Separate destination, duplicate result

**Provider tag**:
A provider-assigned theme such as beach, food, or culture that may support an
assessment but is not an authoritative property of a destination.
_Avoid_: Search criterion, travel intent

**Anywhere**:
A destination scope that does not name a specific place or predefined group of
places.
_Avoid_: Gdziekolwiek, Cały świat, Everywhere

**Indicative price**:
A recently observed minimum for the specified passenger group and complete trip,
used as an estimate when comparing routes or dates; it is not guaranteed to
remain available.
_Avoid_: Live price, offer price

**Price observation**:
The point in time when an indicative price was seen by the provider, distinct
from the time at which exploration retrieves it.
_Avoid_: Search time, retrieval time

**No quote**:
The absence of an indicative price for a route in the provider's available
data; it does not establish that no flight exists.
_Avoid_: No flight, unavailable route

**Live offer**:
A currently available itinerary paired with its total price for the complete
journey.
_Avoid_: Quote, indicative price

**Booking option**:
One complete way to purchase an itinerary, with an authoritative total price;
it may require one or several separate bookings.
_Avoid_: Agent, booking item

**Booking item**:
One separately booked part of a booking option, sold by a particular agent. Its
price is not a complete-trip price when the option has multiple items.
_Avoid_: Booking option, complete offer

**Deal assessment**:
A contextual judgement that a live offer is unusually attractive for the
traveller; price alone need not make an offer a deal.
_Avoid_: Deal filter, price category
