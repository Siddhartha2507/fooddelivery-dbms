// ============================================================
// FOODFLOW - INTERACTIVE DASHBOARD
// ============================================================

document.addEventListener("DOMContentLoaded", () => {

    const navToggle = document.querySelector(".app-nav-toggle");
    const navigation = document.getElementById("appNavigation");
    if (navToggle && navigation) {
        navToggle.addEventListener("click", () => {
            const open = navToggle.getAttribute("aria-expanded") !== "true";
            navToggle.setAttribute("aria-expanded", String(open));
            navToggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
            navigation.classList.toggle("is-open", open);
        });
        navigation.addEventListener("click", event => {
            if (event.target.closest("a")) {
                navigation.classList.remove("is-open");
                navToggle.setAttribute("aria-expanded", "false");
                navToggle.setAttribute("aria-label", "Open navigation");
            }
        });
    }

    // --------------------------------------------------------
    // Animated KPI counters
    // --------------------------------------------------------

    const counters = document.querySelectorAll(".counter");

    counters.forEach(counter => {

        const target = Number(
            counter.dataset.value || 0
        );

        const duration = 1000;
        const startTime = performance.now();

        function animateCounter(currentTime) {

            const elapsed = currentTime - startTime;
            const progress = Math.min(
                elapsed / duration,
                1
            );

            // Smooth ease-out animation
            const easedProgress =
                1 - Math.pow(1 - progress, 3);

            const currentValue =
                target * easedProgress;

            if (target % 1 === 0) {
                counter.textContent =
                    Math.floor(currentValue).toLocaleString("en-IN");
            } else {
                counter.textContent =
                    currentValue.toLocaleString(
                        "en-IN",
                        {
                            maximumFractionDigits: 2
                        }
                    );
            }

            if (progress < 1) {
                requestAnimationFrame(
                    animateCounter
                );
            }
        }

        requestAnimationFrame(
            animateCounter
        );
    });


    // --------------------------------------------------------
    // Search orders
    // --------------------------------------------------------

    const searchInput =
        document.getElementById("orderSearch");

    const statusFilter =
        document.getElementById("statusFilter");

    if (searchInput) {

        searchInput.addEventListener(
            "input",
            filterOrders
        );
    }


    // --------------------------------------------------------
    // Status filter
    // --------------------------------------------------------

    if (statusFilter) {

        statusFilter.addEventListener(
            "change",
            filterOrders
        );
    }

    // --------------------------------------------------------
    // PostgreSQL pgvector semantic menu search
    // --------------------------------------------------------

    const semanticForm = document.getElementById("semanticSearchForm");
    document.querySelectorAll("[data-search-suggestion]").forEach(suggestion => {
        suggestion.addEventListener("click", () => {
            const queryInput = document.getElementById("semanticQuery");
            if (!queryInput) return;
            queryInput.value = suggestion.dataset.searchSuggestion;
            semanticForm?.requestSubmit();
        });
    });
    if (semanticForm) {
        semanticForm.addEventListener("submit", async event => {
            event.preventDefault();
            const input = document.getElementById("semanticQuery");
            const status = document.getElementById("semanticSearchStatus");
            const results = document.getElementById("semanticSearchResults");
            const button = document.getElementById("semanticSearchButton");
            const query = input.value.trim();
            if (!query) {
                status.textContent = "Enter a food search to get started.";
                input.focus();
                return;
            }

            button.disabled = true;
            button.textContent = "Searching…";
            status.textContent = "Searching semantic menu…";
            results.replaceChildren();
            results.classList.add("is-loading");
            try {
                const response = await fetch(`/api/semantic-search?q=${encodeURIComponent(query)}`);
                const payload = await response.json();
                if (!response.ok) throw new Error(payload.error || "Search failed.");
                if (!payload.results.length) {
                    status.textContent = "No matching menu items were returned.";
                    return;
                }
                status.textContent = `${payload.results.length} relevant menu item${payload.results.length === 1 ? "" : "s"} from PostgreSQL`;
                payload.results.forEach(item => {
                    const card = document.createElement("article");
                    card.className = "semantic-result-card";
                    const heading = document.createElement("h3");
                    heading.textContent = item.item_name || "Menu item";
                    const restaurant = document.createElement("p");
                    restaurant.className = "semantic-restaurant";
                    restaurant.textContent = item.restaurant_name || "Restaurant not listed";
                    const details = document.createElement("div");
                    details.className = "semantic-result-details";
                    const price = document.createElement("span");
                    price.textContent = item.price == null ? "Price unavailable" : `₹${Number(item.price).toFixed(2)}`;
                    const availability = document.createElement("span");
                    availability.textContent = item.availability == null ? "Availability unknown" : `Availability: ${item.availability}`;
                    const relevance = document.createElement("span");
                    relevance.textContent = `Similarity ${Number(item.similarity).toFixed(3)}`;
                    details.append(price, availability, relevance);
                    const add = document.createElement("button");
                    add.type = "button";
                    add.className = "small-primary";
                    add.textContent = "Add to cart";
                    add.addEventListener("click", () => addFoodItem(item.item_id));
                    card.append(heading, restaurant, details, add);
                    results.append(card);
                });
            } catch (error) {
                status.textContent = `Unable to search the menu. ${error.message || "Please try again."}`;
            } finally {
                results.classList.remove("is-loading");
                button.disabled = false;
                button.textContent = "Search";
            }
        });
        const initialQuery = document.getElementById("semanticQuery")?.value.trim();
        if (initialQuery) semanticForm.requestSubmit();
    }


    // --------------------------------------------------------
    // Order filtering function
    // --------------------------------------------------------

    function filterOrders() {

        const searchTerm =
            searchInput
                ? searchInput.value
                    .toLowerCase()
                    .trim()
                : "";

        const selectedStatus =
            statusFilter
                ? statusFilter.value
                : "all";

        const rows =
            document.querySelectorAll(
                "#ordersTableBody tr[data-status]"
            );

        let visibleRows = 0;

        rows.forEach(row => {

            const searchData =
                (
                    row.dataset.search || ""
                ).toLowerCase();

            const rowStatus =
                row.dataset.status || "";

            const matchesSearch =
                searchData.includes(searchTerm);

            const matchesStatus =
                selectedStatus === "all" ||
                rowStatus === selectedStatus;

            if (
                matchesSearch &&
                matchesStatus
            ) {

                row.style.display = "";

                visibleRows++;

                // Small animation when result appears
                row.animate(
                    [
                        {
                            opacity: 0.3,
                            transform:
                                "translateX(-4px)"
                        },
                        {
                            opacity: 1,
                            transform:
                                "translateX(0)"
                        }
                    ],
                    {
                        duration: 180,
                        easing: "ease-out"
                    }
                );

            } else {

                row.style.display = "none";
            }

        });


        updateEmptySearchState(
            visibleRows
        );
    }


    // --------------------------------------------------------
    // Empty search result
    // --------------------------------------------------------

    function updateEmptySearchState(
        visibleRows
    ) {

        const tbody =
            document.getElementById(
                "ordersTableBody"
            );

        if (!tbody) return;

        let message =
            document.getElementById(
                "searchEmptyMessage"
            );

        if (visibleRows === 0) {

            if (!message) {

                message =
                    document.createElement("tr");

                message.id =
                    "searchEmptyMessage";

                message.innerHTML = `
                    <td colspan="5"
                        class="empty-state">
                        No matching orders found.
                    </td>
                `;

                tbody.appendChild(message);
            }

        } else {

            if (message) {
                message.remove();
            }
        }
    }


    // --------------------------------------------------------
    // Keyboard shortcut
    // --------------------------------------------------------

    document.addEventListener(
        "keydown",
        event => {

            // "/" focuses search
            if (
                event.key === "/" &&
                document.activeElement.tagName !== "INPUT"
            ) {

                event.preventDefault();

                if (searchInput) {
                    searchInput.focus();
                }
            }

            // Escape clears search
            if (
                event.key === "Escape" &&
                searchInput
            ) {

                searchInput.value = "";

                if (statusFilter) {
                    statusFilter.value = "all";
                }

                filterOrders();
            }
        }
    );

});

// ------------------------------------------------------------
// Customer cart actions (server recalculates prices at checkout)
// ------------------------------------------------------------
async function cartRequest(path, method, payload) {
    const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
    const response = await fetch(path, {
        method,
        headers: {"Content-Type": "application/json", "X-CSRFToken": csrf},
        body: payload === undefined ? undefined : JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "The cart could not be updated.");
    return data;
}

async function addFoodItem(itemId) {
    try {
        await cartRequest("/api/cart/items", "POST", {item_id: Number(itemId), quantity: 1});
        showToast("Added to your cart");
    } catch (error) { showToast(error.message); }
}

document.addEventListener("click", async event => {
    const add = event.target.closest("[data-add-item]");
    if (add) return addFoodItem(add.dataset.addItem);
    const remove = event.target.closest("[data-remove-item]");
    const adjust = event.target.closest("[data-quantity-item]");
    const clear = event.target.closest("[data-clear-cart]");
    try {
        if (clear) await cartRequest("/api/cart", "DELETE");
        else if (remove) await cartRequest(`/api/cart/items/${remove.dataset.removeItem}`, "DELETE");
        else if (adjust) {
            const quantity = Number(adjust.parentElement.querySelector("span").textContent) + Number(adjust.dataset.quantityChange);
            await cartRequest(`/api/cart/items/${adjust.dataset.quantityItem}`, "PATCH", {quantity: Math.max(0, quantity)});
        } else return;
        location.reload();
    } catch (error) { showToast(error.message); }
});


// ============================================================
// REFRESH DASHBOARD
// ============================================================

async function refreshDashboard() {

    const button =
        document.querySelector(
            ".refresh-btn"
        );

    if (button) {

        button.disabled = true;

        button.textContent =
            "↻ Refreshing...";
    }


    try {

        const response =
            await fetch(
                "/api/dashboard",
                {
                    cache: "no-store"
                }
            );

        if (!response.ok) {
            throw new Error(
                "Dashboard API request failed"
            );
        }


        const data =
            await response.json();


        // ----------------------------------------------------
        // Update counters
        // ----------------------------------------------------

        updateCounter(
            "Total Orders",
            data.total_orders
        );


        updateCounter(
            "Customers",
            data.total_customers
        );


        updateCounter(
            "Restaurants",
            data.total_restaurants
        );


        // ----------------------------------------------------
        // Show refresh notification
        // ----------------------------------------------------

        showToast(
            "✓ Dashboard data refreshed from MongoDB Atlas"
        );


    } catch (error) {

        console.error(error);

        showToast(
            "⚠ Unable to refresh dashboard"
        );

    } finally {

        if (button) {

            button.disabled = false;

            button.textContent =
                "↻ Refresh";
        }
    }
}


// ============================================================
// Update a counter
// ============================================================

function updateCounter(
    label,
    value
) {

    const cards =
        document.querySelectorAll(
            ".kpi-card"
        );

    cards.forEach(card => {

        const cardLabel =
            card.querySelector(
                ".kpi-label"
            );

        const counter =
            card.querySelector(
                ".counter"
            );

        if (
            cardLabel &&
            counter &&
            cardLabel.textContent
                .trim()
                .toLowerCase() ===
            label.toLowerCase()
        ) {

            counter.dataset.value =
                value;

            animateSingleCounter(
                counter,
                value
            );
        }
    });
}


// ============================================================
// Animate individual counter
// ============================================================

function animateSingleCounter(
    element,
    target
) {

    const start =
        Number(
            element.textContent
                .replace(/,/g, "")
                .replace("₹", "") || 0
        );

    const duration = 600;
    const startTime = performance.now();

    function frame(now) {

        const progress =
            Math.min(
                (now - startTime) /
                duration,
                1
            );

        const eased =
            1 - Math.pow(
                1 - progress,
                3
            );

        const value =
            start +
            (target - start) *
            eased;

        element.textContent =
            Math.round(value)
                .toLocaleString("en-IN");

        if (progress < 1) {

            requestAnimationFrame(frame);
        }
    }

    requestAnimationFrame(frame);
}


// ============================================================
// Toast notification
// ============================================================

function showToast(message) {

    const toast =
        document.getElementById(
            "toast"
        );

    if (!toast) return;

    toast.textContent =
        message;

    toast.classList.add(
        "show"
    );

    clearTimeout(
        window.foodFlowToastTimer
    );

    window.foodFlowToastTimer =
        setTimeout(() => {

            toast.classList.remove(
                "show"
            );

        }, 2800);
}
