import { Bot } from "lucide-react";
import { ComingSoon } from "@/components/ComingSoon";

export default function Page() {
  return (
    <ComingSoon
      icon={Bot}
      title="K-BOT — Assistant flotte intelligent"
      description="K-BOT est disponible partout via le bouton flottant en bas à droite. Il répond à vos questions à partir des données autorisées de votre périmètre."
      features={[
        "Véhicules & chauffeurs disponibles",
        "Coûts et consommation",
        "Maintenances à prévoir",
        "Résumé & synthèse du jour",
      ]}
    />
  );
}
