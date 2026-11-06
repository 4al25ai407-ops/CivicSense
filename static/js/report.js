/* GPS + map picker for the report form.
   Uses high-accuracy GPS, keeps the best reading, shows accuracy, and lets the user drag the pin to the exact spot. */
(function () {
  var mapEl = document.getElementById('map');
  if (!mapEl || typeof L === 'undefined') return;

  var latIn = document.getElementById('latitude');
  var lonIn = document.getElementById('longitude');
  var locIn = document.getElementById('location');
  var btn = document.getElementById('gps-btn');
  var status = document.getElementById('gps-status');
  var statusText = document.getElementById('gps-text');

  var MYSURU = [12.2958, 76.6394];
  var map = L.map(mapEl, { zoomControl: true }).setView(MYSURU, 13);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);

  var pinIcon = L.divIcon({ className: 'pin', html: '<span></span>', iconSize: [28, 28], iconAnchor: [14, 28] });
  var marker = null, circle = null, watchId = null, timer = null, geoTimer = null, best = null;

  function setStatus(kind, text) {
    status.className = 'gps-status ' + kind;
    statusText.textContent = text;
  }

  function placeMarker(lat, lon, acc) {
    var ll = [lat, lon];
    if (!marker) {
      marker = L.marker(ll, { icon: pinIcon, draggable: true }).addTo(map);
      marker.on('dragend', function () {
        var p = marker.getLatLng();
        if (circle) { map.removeLayer(circle); circle = null; }
        commit(p.lat, p.lng);
        setStatus('good', 'Pin moved manually to the exact spot.');
        lookupAddress(p.lat, p.lng);
      });
    } else { marker.setLatLng(ll); }
    if (circle) { map.removeLayer(circle); circle = null; }
    if (acc) {
      circle = L.circle(ll, { radius: acc, color: '#d4af37', weight: 1, fillColor: '#d4af37', fillOpacity: 0.12 }).addTo(map);
    }
  }

  function commit(lat, lon) {
    latIn.value = lat.toFixed(6);
    lonIn.value = lon.toFixed(6);
  }

  function lookupAddress(lat, lon) {
    clearTimeout(geoTimer);
    geoTimer = setTimeout(function () {
      fetch('/api/reverse-geocode?lat=' + lat + '&lon=' + lon, { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : Promise.reject(); })
        .then(function (d) { if (d.address) { locIn.value = d.address; } })
        .catch(function () {
          if (!locIn.value.trim()) { locIn.value = lat.toFixed(5) + ', ' + lon.toFixed(5); }
        });
    }, 300);
  }

  function finish() {
    if (watchId !== null) { navigator.geolocation.clearWatch(watchId); watchId = null; }
    clearTimeout(timer);
    btn.disabled = false;
    btn.textContent = 'Use my current location';
    if (!best) return;
    var acc = Math.round(best.accuracy);
    commit(best.lat, best.lon);
    placeMarker(best.lat, best.lon, best.accuracy);
    map.fitBounds(circle ? circle.getBounds() : L.latLngBounds([[best.lat, best.lon]]), { maxZoom: 18, padding: [30, 30] });
    if (acc <= 30) setStatus('good', 'Precise GPS fix: accurate to about ' + acc + ' m. You can still drag the pin to fine-tune.');
    else if (acc <= 120) setStatus('fair', 'Location accurate to about ' + acc + ' m. Drag the pin to the exact spot if needed.');
    else setStatus('bad', 'Only an approximate location (about ' + acc + ' m). Laptops without GPS use Wi-Fi/IP. Drag the pin to the exact spot, or use your phone for GPS accuracy.');
    lookupAddress(best.lat, best.lon);
  }

  function start() {
    if (!window.isSecureContext) {
      setStatus('bad', 'GPS needs a secure page. Open the site over https:// (or http://127.0.0.1 on this computer).');
      return;
    }
    if (!navigator.geolocation) { setStatus('bad', 'This browser does not support location.'); return; }
    best = null;
    btn.disabled = true;
    btn.textContent = 'Locating…';
    setStatus('busy', 'Getting a precise GPS fix. Please allow location access and wait a few seconds…');
    watchId = navigator.geolocation.watchPosition(function (pos) {
      var c = pos.coords;
      if (!best || c.accuracy < best.accuracy) {
        best = { lat: c.latitude, lon: c.longitude, accuracy: c.accuracy };
        placeMarker(best.lat, best.lon, best.accuracy);
        map.setView([best.lat, best.lon], 17);
        setStatus('busy', 'Improving accuracy… currently about ' + Math.round(c.accuracy) + ' m');
      }
      if (c.accuracy <= 15) finish();
    }, function (err) {
      var msg = {
        1: 'Location permission was denied. Allow location for this site in your browser settings, then try again.',
        2: 'Your device could not determine its location. Turn on GPS/Location services and try again.',
        3: 'Locating timed out. Try again outside or near a window.'
      }[err.code] || 'Could not get your location.';
      if (err.code === 3 && best) { finish(); return; }
      if (watchId !== null) { navigator.geolocation.clearWatch(watchId); watchId = null; }
      clearTimeout(timer);
      btn.disabled = false; btn.textContent = 'Use my current location';
      setStatus('bad', msg);
    }, { enableHighAccuracy: true, maximumAge: 0, timeout: 25000 });
    timer = setTimeout(finish, 20000); // stop refining after 20 s and keep the best reading
  }

  btn.addEventListener('click', start);

  // Tap/click the map to place the pin manually
  map.on('click', function (e) {
    placeMarker(e.latlng.lat, e.latlng.lng, null);
    commit(e.latlng.lat, e.latlng.lng);
    setStatus('good', 'Pin placed manually on the map.');
    lookupAddress(e.latlng.lat, e.latlng.lng);
  });

  // Restore a pin if the form re-renders with coordinates (validation error)
  if (latIn.value && lonIn.value && !isNaN(parseFloat(latIn.value))) {
    var la = parseFloat(latIn.value), lo = parseFloat(lonIn.value);
    placeMarker(la, lo, null); map.setView([la, lo], 17);
    setStatus('good', 'Saved pin location restored.');
  }

  // Photo preview
  var photo = document.getElementById('photo'), prev = document.getElementById('preview');
  if (photo && prev) {
    photo.addEventListener('change', function () {
      var f = photo.files && photo.files[0];
      if (!f) { prev.style.display = 'none'; return; }
      prev.src = URL.createObjectURL(f); prev.style.display = 'block';
    });
  }
})();
