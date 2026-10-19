**Demo Shop: Order Service**

**Software Requirements Specification**

**Version 0.3**

## Approval Page

| Role | Name | Date |
|---|---|---|
| BA | Alice | 2026-10-01 |
| QA | Bao | 2026-10-02 |

## Revision History

| Version | Date | Change |
|---|---|---|
| 0.1 | 2026-09-20 | First draft |
| 0.3 | 2026-10-01 | Payment retries |

# 1. Introduction

## 1.1. Purpose

This document describes the order service of a small online bookshop: placing an order, paying for it and returning
a book.

## 1.2. Definitions, Acronyms and Abbreviations

| Term | Meaning |
|---|---|
| SKU | One book in one format |
| Hold | Stock kept for an order that is not paid yet |

# 2. Use Case Specifications

## 2.1. Orders

### Actors

| Actor | Description |
|---|---|
| Customer | A person who buys books |
| Payment gateway | The service that takes card payments |

#### UC ORD-1: Place an order

| Item | Content |
|---|---|
| ID | UC-ORD-001 (ORD-01) |
| Status | Approved |
| Objective | The customer turns a cart into an order |
| Actor | Customer |
| Trigger | The customer presses Place order |
| Pre-condition | The cart holds at least one line |
| Post-condition | The order exists and its stock is held |

**Activity Flow**

| Step | Actor | System |
|---|---|---|
| 1 | Reviews the cart and presses Place order | |
| 2 | | Checks the cart (BR ORD1-1) |
| 3 | | Creates the order and holds its stock (BR ORD1-2) |

##### Step (2): BR ORD1-1 – Cart rules

- A cart holds at most 50 lines and 99 of one SKU.
- An item out of stock cannot be added; the shop shows "Out of stock".

##### Step (3): BR ORD1-2 – Stock reservation

- Placing the order holds its stock for 15 minutes.
- Unpaid after 15 minutes: the hold ends and the order is Expired.

#### UC ORD-2: Pay for an order

| Item | Content |
|---|---|
| ID | UC-ORD-002 (ORD-02) |
| Objective | The customer pays for a placed order |
| Actor | Customer, Payment gateway |
| Trigger | The customer picks a payment method |

**Activity Flow**

| Step | Actor | System |
|---|---|---|
| 1 | Picks a payment method and pays | |
| 2 | | Sends the payment to the gateway |
| 2.1 | | On failure, lets the customer retry (BR ORD2-1) |
| 3 | | Marks the order Paid |

##### Steps (2, 2.1): BR ORD2-1 – Payment retries

A failed payment can be retried 3 times within 10 minutes. The gateway answers with a code such as:

```text
# declined: insufficient funds
```

After the third failure the order is Payment failed and its stock hold ends.

## 2.2. Returns

### UC RET-1: Return a book

| Item | Content |
|---|---|
| ID | UC-RET-001 |
| Objective | The customer sends a book back within 14 days |
| Actor | Customer |

#### BR RET1-1 – Return window

A book is returned within 14 days of delivery, in the state it arrived.

# 3. Mockups Screen

## 3.1. Site Map

Home, then Cart, then Checkout.

### Screen: Cart

| Component | Behaviour |
|---|---|
| Line list | Shows each line with its quantity |
| Place order | Starts UC ORD-1 |

### Screen: Checkout

| Component | Behaviour |
|---|---|
| Payment method | Card or wallet |

# 4. Common Business Rules

| ID | Rule |
|---|---|
| CBR 1 | Prices are shown with tax |
| CBR 2 | Times are shown in the customer's time zone |

# 5. Non-Functional Requirements

| ID | Group | Requirement |
|---|---|---|
| NFR-SEC-01 | Security | Card data never reaches the shop's servers |
| NFR-PERF-01 | Performance | Placing an order answers within 2 seconds |

Each requirement is checked before a release.

# 6. Integration and Deployment

The service runs behind the shop's API gateway.

# 7. Open Items

- Gift wrapping is not decided.
