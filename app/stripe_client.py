"""Configuration unique du client Stripe. Tout module qui parle à Stripe importe
`stripe` D'ICI, jamais directement.

Sans ce point de passage obligé, la clé n'était posée que dans app/alertes.py : tout
module qui n'importait pas alertes (app/volume.py, app/abonnes.py, un outil en ligne
de commande) trouvait `stripe.api_key` vide et abandonnait en silence, en rendant
None comme si Stripe était inaccessible. Le symptôme — « permission manquante »
alors que la permission était bien accordée — ne désignait jamais la vraie cause.
"""
from __future__ import annotations

import stripe
from stripe import _http_client

from app import config

stripe.api_key = config.STRIPE_API_KEY

# Par défaut, la bibliothèque attend 80 s par appel et réessaie 2 fois : un seul
# enrichissement pourrait bloquer 4 minutes. Or on est sur le chemin critique du
# webhook, AVANT que l'évènement soit marqué comme traité — Stripe abandonnerait la
# livraison bien avant et la rejouerait, faisant partir deux alertes identiques.
stripe.max_network_retries = 0
stripe.default_http_client = _http_client.new_default_http_client(
    timeout=config.STRIPE_TIMEOUT
)

# Second client, pour les tâches de fond (MRR, volume net, rattrapage). Elles
# parcourent des dizaines de pages ; avec les réglages du webhook (0 nouvelle
# tentative, 8 s) une seule page en échec sur 41 faisait tout recommencer — soit,
# à 0,5 % d'échec par requête, près d'un calcul sur cinq perdu. Ici, rien n'est sur
# le chemin critique : on peut se permettre d'insister.
client_fond = stripe.StripeClient(
    config.STRIPE_API_KEY or "sk_absent",
    max_network_retries=2,
    http_client=_http_client.new_default_http_client(timeout=30),
)

__all__ = ["stripe", "client_fond"]
