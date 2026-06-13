"use client";

import FullCalendar from "@fullcalendar/react";
import dayGridPlugin from "@fullcalendar/daygrid";
import timeGridPlugin from "@fullcalendar/timegrid";
import listPlugin from "@fullcalendar/list";
import frLocale from "@fullcalendar/core/locales/fr";
import type { EventInput } from "@fullcalendar/core";

export default function CalendarView({
  events,
  onEventClick,
}: {
  events: EventInput[];
  /** Détail d'un événement : reçoit l'`extendedProps.id` de l'événement cliqué. */
  onEventClick?: (id: string) => void;
}) {
  return (
    <div className="kx-fc">
      <FullCalendar
        plugins={[dayGridPlugin, timeGridPlugin, listPlugin]}
        initialView="timeGridWeek"
        locale={frLocale}
        firstDay={1}
        headerToolbar={{
          left: "prev,next today",
          center: "title",
          right: "dayGridMonth,timeGridWeek,listWeek",
        }}
        buttonText={{ today: "Aujourd'hui", month: "Mois", week: "Semaine", list: "Liste" }}
        events={events}
        height="72vh"
        nowIndicator
        allDaySlot={false}
        slotMinTime="05:00:00"
        slotMaxTime="22:00:00"
        eventTimeFormat={{ hour: "2-digit", minute: "2-digit", hour12: false }}
        dayMaxEvents={3}
        eventClick={(info) => {
          const id = info.event.extendedProps?.id as string | undefined;
          if (id && onEventClick) onEventClick(id);
        }}
      />
    </div>
  );
}
