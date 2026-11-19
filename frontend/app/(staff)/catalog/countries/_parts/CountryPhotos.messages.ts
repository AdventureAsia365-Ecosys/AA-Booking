// app/(staff)/catalog/countries/_parts/CountryPhotos.messages.ts
// AA-1128 (B5): the English and Vietnamese text of CountryPhotos.tsx (kit core/i18n.ts). The keys are the English
// text; the phrasebook registers the Vietnamese side for text kept in module-level tables (ui/local/phrasebook.ts).
import { defineMessages } from "@/ui/core/i18n";
import { phrasebook } from "@/ui/local/phrasebook";

export const MSG = phrasebook(
  defineMessages(
  {
    "{0} cover photo": (...a: unknown[]) => `${a[0]} cover photo`,
    "{0}Cover from CIS": (...a: unknown[]) => `${a[0]}Cover from CIS`,
    "CIS has no photo for {0} yet.": (...a: unknown[]) => `CIS has no photo for ${a[0]} yet.`,
    "Photos": "Photos",
    "The first {0} of {1} CIS photos of its places and tours.": (...a: unknown[]) => `The first ${a[0]} of ${a[1]} CIS photos of its places and tours.`,
    "CIS photos of its places and tours.": "CIS photos of its places and tours.",
    "Open CIS photos": "Open CIS photos",
    "No CIS photos for {0} yet. Photos are added and matched to places in CIS; they show here as soon as CIS publishes them.": (...a: unknown[]) => `No CIS photos for ${a[0]} yet. Photos are added and matched to places in CIS; they show here as soon as CIS publishes them.`,
    "Open photo {0}{1}": (...a: unknown[]) => `Open photo ${a[0]}${a[1]}`,
    "{0} · photo {1} of {2}": (...a: unknown[]) => `${a[0]} · photo ${a[1]} of ${a[2]}`,
    "{0} photo {1}": (...a: unknown[]) => `${a[0]} photo ${a[1]}`,
    "No photo": "No photo",
    "Previous photo": "Previous photo",
    "Next photo": "Next photo",
    "Tour photo": "Tour photo",
    "Open destination": "Open destination",
  },
  {
    "{0} cover photo": (...a: unknown[]) => `Ảnh bìa ${a[0]}`,
    "{0}Cover from CIS": (...a: unknown[]) => `${a[0]}Ảnh bìa từ CIS`,
    "CIS has no photo for {0} yet.": (...a: unknown[]) => `CIS chưa có ảnh cho ${a[0]}.`,
    "Photos": "Ảnh",
    "The first {0} of {1} CIS photos of its places and tours.": (...a: unknown[]) => `${a[0]} ảnh CIS đầu tiên trên ${a[1]} của các địa điểm và tour.`,
    "CIS photos of its places and tours.": "Ảnh CIS của các địa điểm và tour.",
    "Open CIS photos": "Mở ảnh CIS",
    "No CIS photos for {0} yet. Photos are added and matched to places in CIS; they show here as soon as CIS publishes them.": (...a: unknown[]) => `Chưa có ảnh CIS cho ${a[0]}. Ảnh được thêm và gắn với địa điểm trong CIS; chúng hiện ở đây ngay khi CIS xuất bản.`,
    "Open photo {0}{1}": (...a: unknown[]) => `Mở ảnh ${a[0]}${a[1]}`,
    "{0} · photo {1} of {2}": (...a: unknown[]) => `${a[0]} · ảnh ${a[1]} trên ${a[2]}`,
    "{0} photo {1}": (...a: unknown[]) => `${a[0]} ảnh ${a[1]}`,
    "No photo": "Không có ảnh",
    "Previous photo": "Ảnh trước",
    "Next photo": "Ảnh sau",
    "Tour photo": "Ảnh tour",
    "Open destination": "Mở điểm đến",
  },
  ),
);
