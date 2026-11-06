/* Read-only map showing where an issue was reported. */
(function () {
  var el = document.getElementById('view-map');
  if (!el || typeof L === 'undefined') return;
  var lat = parseFloat(el.dataset.lat), lng = parseFloat(el.dataset.lng);
  if (isNaN(lat) || isNaN(lng)) return;
  var map = L.map(el, { scrollWheelZoom: false }).setView([lat, lng], 17);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
  L.marker([lat, lng], { icon: L.divIcon({ className: 'pin', html: '<span></span>', iconSize: [28, 28], iconAnchor: [14, 28] }) }).addTo(map);
})();
