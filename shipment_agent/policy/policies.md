# Delays and customer updates
<!-- id: delay-comms; topic: delay; carrier: all -->
If a shipment has no tracking scan for 48 hours, or is past its expected delivery date, inform the customer proactively with a revised estimate. Add 2 days to the original EDD for surface shipments and 1 day for air. Do not promise a specific time of day. Send at most one delay message per case unless the revised date also passes.

# Suspected lost shipments
<!-- id: lost-verify; topic: lost; carrier: all -->
A shipment is only treated as lost after at least 7 days with no scan AND a trace request (carrier inquiry) has been raised. Until the carrier responds or 72 hours pass after the inquiry, tell the customer we are investigating; never tell them the parcel is lost. After the carrier confirms loss, offer the customer a choice of reshipment or full refund.

# Damaged in transit
<!-- id: damaged; topic: damaged; carrier: all -->
When the carrier reports a shipment as damaged, the default resolution is a free reshipment of the same items. If the item is out of stock, offer a full refund instead. Reshipments always require approval from a support agent. File a damage claim with the carrier for every damaged shipment.

# Refund approval limits
<!-- id: refund-limits; topic: refund; carrier: all -->
Every refund needs approval from a support agent. Refunds above ₹5,000 additionally need a support manager's approval. Refunds for COD orders that were never delivered are not applicable, since no payment was collected; cancel the order instead.

# Failed delivery attempts
<!-- id: failed-attempt; topic: failed_attempt; carrier: all -->
After a failed delivery attempt, message the customer within 2 hours asking for a new delivery slot or a pickup point. If the customer does not reply within 24 hours, send one reminder. If there is still no reply after 48 hours, let the carrier's own reattempt run its course and escalate COD orders to support.

# Delhivery: reattempts and address changes
<!-- id: delhivery-caps; topic: carrier_capability; carrier: delhivery -->
Delhivery allows up to 3 delivery reattempts, requested via the NDR update API. Address changes are allowed while the shipment is in transit, as long as the new address is within the same city and the shipment is not yet out for delivery. Changes to a different city require an RTO and a new shipment.

# Blue Dart: reattempts and address changes
<!-- id: bluedart-caps; topic: carrier_capability; carrier: bluedart -->
Blue Dart allows up to 2 delivery reattempts. Blue Dart does not allow address changes in transit; if the address is wrong, the shipment must be returned to origin (RTO) and re-shipped to the corrected address. Shipments are held at the destination branch for 3 days after the final failed attempt before RTO.

# High-value orders
<!-- id: high-value; topic: escalation; carrier: all -->
Orders with a value above ₹25,000 must be escalated to a support agent for any exception other than a minor delay. The agent may still gather information and draft a recommendation, but should not message the customer about anything other than a delay.

# Unclear or unrecognised carrier status
<!-- id: unclear; topic: escalation; carrier: all -->
If the carrier status is unrecognised, contradictory, or the cause of the exception cannot be determined from the tracking data and carrier notes, escalate to a support agent with a summary of what was found. Do not guess.
