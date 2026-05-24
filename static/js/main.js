// Utilidades globales
function formatNumber(n) {
    if (n === null || n === undefined || n === '') return '';
    let num = parseFloat(n);
    if (isNaN(num)) return n;
    if (num === 0) return '';
    let s = Math.abs(num).toLocaleString('es-CL');
    return num < 0 ? '(' + s + ')' : s;
}

function getEmpresa() {
    return JSON.parse(sessionStorage.getItem('empresa') || 'null');
}

function setEmpresa(emp) {
    sessionStorage.setItem('empresa', JSON.stringify(emp));
    actualizarSidebarEmpresa();
}

function actualizarSidebarEmpresa() {
    const emp = getEmpresa();
    const el = document.getElementById('sidebar-empresa');
    if (el) {
        if (emp) {
            el.innerHTML = `<strong>${emp.nombre}</strong><br><small class="text-white-50">${emp.rut}</small>`;
        } else {
            el.innerHTML = '<span class="text-white-50">Sin empresa seleccionada</span>';
        }
    }
}

function showToast(message, type = 'success') {
    const toastContainer = document.getElementById('toast-container');
    if (!toastContainer) return;
    const toast = document.createElement('div');
    toast.className = `toast align-items-center text-white bg-${type === 'success' ? 'success' : type === 'error' ? 'danger' : 'warning'}`;
    toast.setAttribute('role', 'alert');
    toast.innerHTML = `<div class="d-flex"><div class="toast-body">${message}</div><button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>`;
    toastContainer.appendChild(toast);
    const bsToast = new bootstrap.Toast(toast, { delay: 3000 });
    bsToast.show();
    toast.addEventListener('hidden.bs.toast', () => toast.remove());
}

function apiGet(url) {
    return fetch(url).then(r => r.ok ? r.json() : Promise.reject(r.statusText));
}

function apiPost(url, data) {
    return fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    }).then(r => r.ok ? r.json() : Promise.reject(r.statusText));
}

function apiPostForm(url, formData) {
    return fetch(url, {
        method: 'POST',
        body: formData
    }).then(r => r.ok ? r.json() : Promise.reject(r.statusText));
}

// Inicializar al cargar
document.addEventListener('DOMContentLoaded', () => {
    actualizarSidebarEmpresa();
});
