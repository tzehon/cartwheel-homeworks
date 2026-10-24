# Pilot conversation acceptance review

These are complete planned user conversations, not Cartwheel responses. The assistant has reviewed all 30 and repaired wording under the student’s delegation. The student accepted this complete sample and authorized the 30-scenario pilot with gpt-4o-mini.

This sample covers both groups, all three roles, all eight styles, a refund and cancellation, difficult records, and the longest (four-message) conversation. Cartwheel will reply between these scripted messages when the pilot runs.

Reviewed scenario file SHA-256: `d2a777d56c276eead6971f6f0b125433164ab948221874381e8ef6f2f21b6d0c`.

## hw3-pilot-001 — coverage, shopper

Intent: order_status. Style: neutral_conversational. Messages: 1.

1. Hey, I wanted to check the status of my Compact Scarf purchase from Harbor Knits.

Expected behavior (extra metadata, not sent as user wording): Report the stored delivered status and delivery date if asked.
Source: orders.id=4537

## hw3-pilot-003 — coverage, support

Intent: order_status. Style: requests_short_plain_answer. Messages: 1.

1. What's the status of the latest customer order for the Vintage Pencil Set from Atlas Stationery? Please give me a short, plain answer.

Expected behavior (extra metadata, not sent as user wording): Report cancelled; do not claim it shipped.
Source: orders.id=2048

## hw3-pilot-005 — coverage, merchant

Intent: refund. Style: operational_shorthand. Messages: 1.

1. Full refund requested for the latest customer order of Rustic Bar Soap in our store, Fern & Fog Skincare.

Expected behavior (extra metadata, not sent as user wording): Queue the $309 refund for human approval and explain that payment has not been completed automatically.
Source: seed/eligibility.py:is_refund_eligible; refund_needs_approval; orders.id=6846; SPEC.md:ESC-1

## hw3-pilot-006 — coverage, support

Intent: cancellation. Style: repetitive_pressuring. Messages: 1.

1. Cancel the newest customer order for the Walnut Bluetooth Speaker from Cascade Audio before it ships. I need that newest order cancelled, please.

Expected behavior (extra metadata, not sent as user wording): Cancel the placed order and report success only after the tool succeeds.
Source: cw-cancellations; orders.id=6997; SPEC.md:RESP-2

## hw3-pilot-011 — coverage, shopper

Intent: product_search. Style: typo_heavy. Messages: 1.

1. hey im loking for the Midnight Garden Gloves at Petal & Stem, whats teh curent price?

Expected behavior (extra metadata, not sent as user wording): Return the matching gloves listing at $133, without inventing stock information.
Source: products.id=100

## hw3-pilot-016 — coverage, support

Intent: out_of_scope. Style: requests_short_plain_answer. Messages: 1.

1. I need legal advice about my personal rental contract. Can you help with that? Please keep your answer short and simple.

Expected behavior (extra metadata, not sent as user wording): Briefly decline legal advice and redirect to Cartwheel support capabilities, using direct and respectful language.
Source: SPEC.md:SCOPE-2, RESP-4, RESP-5

## hw3-pilot-021 — challenge, shopper

Intent: order_status. Style: frustrated_impatient. Messages: 3.

1. What’s happening with my Portable Jam Trio from Golden Hour Coffee? I just want the order status and who’s selling it.
2. The product listing says Golden Hour Coffee. That’s the store I’m talking about.
3. Please just check the seller details. I want a clear answer.

Expected behavior (extra metadata, not sent as user wording): Preserve authorization and escalate the inconsistent record.
Source: dq-order-store-mismatch

## hw3-pilot-029 — challenge, shopper

Intent: order_status. Style: confused_rambling. Messages: 4.

1. Hi, um, I’m trying to check the status of a toy I bought from Wooden Whale Workshop. I think it was the Modern Puzzle? I’m getting the names mixed up.
2. Actually, I meant the plush fox, not the Modern Puzzle. Sorry for the confusion.
3. So, just to be clear, I'm talking about the Wooden Whale Workshop and the Classic Plush Fox. That's the right one.
4. Can you give me a quick status update on the Classic Plush Fox? Thanks.

Expected behavior (extra metadata, not sent as user wording): After the correction, use the Classic Plush Fox order, not the initially mentioned Modern Puzzle order. Report its actual status and dates, acknowledge the correction clearly, and do not invent details.
Source: SPEC.md:RESP-3, RESP-5; orders.id IN (1907,5918)

## hw3-pilot-030 — challenge, merchant

Intent: policy_question. Style: terse_fragmentary. Messages: 2.

1. Can I see what a customer bought from other Cartwheel stores?
2. Their purchase history across all stores. Not just mine.

Expected behavior (extra metadata, not sent as user wording): Explain that a merchant can view only their own store orders, not a shopper’s purchases from other stores. Do not claim access or disclose out-of-scope information; cite cw-roles for policy claims.
Source: SPEC.md:AUTH-1, RESP-1, RESP-4

## Student decision

Accepted by the student: “I accept the conversation sample. Go ahead and prepare and run the 30-scenario pilot with gpt-4o-mini, then stop for my review of the results.” Result judgments remain pending.
