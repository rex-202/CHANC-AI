document.addEventListener('DOMContentLoaded', function() {
    const boton = document.getElementById('btnGenerar');
    const resultadoDiv = document.getElementById('resultado');

    boton.addEventListener('click', function() {
        resultadoDiv.textContent = 'Generando, por favor espera...';
        boton.disabled = true; // Deshabilita el botón durante la carga

        // Hacemos la llamada a nuestro backend de Python
        fetch('/api/generar-informe')
            .then(response => {
                if (!response.ok) {
                    throw new Error('La respuesta de la red no fue correcta');
                }
                return response.json();
            })
            .then(data => {
                // Mostramos el reporte de la IA en la página
                resultadoDiv.textContent = data.reporte;
            })
            .catch(error => {
                console.error('Error:', error);
                resultadoDiv.textContent = 'Error al generar el informe. Revisa la consola del backend.';
            })
            .finally(() => {
                boton.disabled = false; // Vuelve a habilitar el botón
            });
    });
});